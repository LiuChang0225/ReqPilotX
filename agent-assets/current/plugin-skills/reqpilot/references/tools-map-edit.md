# Incremental Map authoring

Use the current integer source refs rather than retyping long source IDs.
A physical `table_id` is not a `source_node_id`; select the table entry's source
ref. Revise the retained authoring decisions, not a hand-copied reconstruction
of the compiled Map. For a large request, use the existing typed CLI with
`--input <request.json>` and patch only the affected JSON values. Re-read a
rejected reference against the current source; never guess a near-matching ID.

Use `compile-map` with a `reqpilot-map-edit/v1` input when this capability is listed in `reqpilot_context.map_authoring`. It produces the same Map v2 decisions and DocumentMapArtifactV1 as full authoring. It does not choose semantic boundaries or mark the result accepted by a person.

Read actual evidence first. Prefer `inspect-source` with `view:"authoring"`, `source_ir` and a `page`, `query` (source substring), or `capture_index` selection. Returned `ref` / `capture_index` is ready for Map authoring. `raw_text` is complete, table cells keep `row`, `column`, `row_span`, `column_span`, `raw_text`; `full_source_read` and `next_read` provide exact further reads. Query matches include text and physical tables independently, not automatic ownership. No full table schema inspection or native directory discovery is needed for an ordinary text scope.

Use `limit` for result count (`max_results` is an equivalent alias on updated hosts). To read neighboring context, select its page or source indexes; `context_before`/`context_after` are not implemented. For a parsed JSON root use `json_pointer:""`; `/` means an empty-name member. Follow the returned read/correction arguments instead of trying the same unsupported fields against more query terms.

The existing **capture_index** column in navigation TSVs and complete source IDs also work. This is a zero-based SourceIR index, not a PDF page number, source_order, or TSV row number. For text inside one block, select a unique exact quote, or unique `start` / `end_before` markers. The program computes character offsets and copies unchanged source text/geometry; ambiguous markers fail with an actionable error. When several query terms return the same complete source ref, reuse that evidence; another identical query adds no new source unless the input changed. A crop at a different region/scale can still resolve a visual question.

For example, suppose capture index 8 contains exactly:

```text
1.1 Activation
The unit shall start.
1.2 Shutdown
The unit shall stop.
```

Save this candidate and run `compile-map` with the original `source`, `source_ir`, candidate `input` and a fresh `output_dir`:

```json
{
  "schema_version": "reqpilot-map-edit/v1",
  "nodes": [
    {"node_id": "doc", "parent_id": null, "kind": "DOCUMENT"},
    {"node_id": "activation", "parent_id": "doc", "kind": "SECTION",
     "heading": {"source": {"source": 8, "quote": "1.1 Activation"}, "label": "1.1"},
     "content": [{"source": 8, "start": "The unit shall start.", "end_before": "1.2 Shutdown"}]},
    {"node_id": "shutdown", "parent_id": "doc", "kind": "SECTION",
     "heading": {"source": {"source": 8, "quote": "1.2 Shutdown"}, "label": "1.2"},
     "content": [{"source": 8, "start": "The unit shall stop."}]}
  ]
}
```

`heading.source` can also be a whole index/ID. `label` is an explicit exact prefix selected by the Agent; the program separates it from the title without inferring numbering. Internal line breaks are preserved. A DOCUMENT without an explicit heading uses the original filename. `content` accepts text selectors or whole indexes/IDs (including images); supporting figures need this owning content or a whole image ref in `numbered_body.continuations`, not only `evidence_refs` ([positive/negative example](tools-images.md#supporting-figure-map-binding)). Tables use `logical_tables` with existing fields and fragments `{source_node_id: <index-or-ID>, row_range: [lo,hi]}`. Full `[lo,hi)` is optional when retaining an entire physical table. Table roles, ownership and meaningful row boundaries remain Agent decisions.

If a numbered clause has **no separate heading**, use `numbered_body` instead of inventing a heading from its obligation. For example, if source 12 contains `2、The unit shall retain its setting.\n3、...`, the node can be:

```json
{"node_id":"setting","parent_id":"governing-section","kind":"SECTION",
 "numbered_body":{"source":{"source":12,"start":"2、","end_before":"3、"},"label":"2、"}}
```

The compiler uses only the actual number as structural label/title evidence and retains the entire remaining clause as body. It computes the offsets; do not supply heading/content fields with `numbered_body`. The Agent still decides whether the numbered group is a separate clause or one complete BOM/test. Governing conditions stay on the actual parent. When a heading starts on an earlier page, continue contextual lookup until its parent is evidenced or explicitly unresolved; a local scope does not justify inventing a flat original hierarchy.

If that numbered body continues in other captured blocks/pages, add `continuations:[<source-selector>, ...]` inside `numbered_body`. Select the actual continuation, ending before the next unrelated heading when they share a capture block. The tool binds those extra ranges to the same clause; it does not infer cross-page ownership or require a new item for each block.

After the first meaningful scope is saved, pass its output directory as `map_dir`. Send only changed fields, keyed by `node_id` or table `scope_id`; omitted nodes/tables/fields are retained. Example after checking that a table belongs to the shutdown scope:

```json
{"schema_version":"reqpilot-map-edit/v1",
 "logical_tables":[{"scope_id":"observed-table","owner_node_id":"shutdown"}]}
```

This example requires an existing table named `observed-table`; it does not create table evidence. Lists supplied on an existing object replace that field completely. `content` replaces both content source arrays; `heading` replaces title/label/title evidence together. Do not combine these convenience fields with their explicit counterparts in one edit. Optional node fields may be removed with `unset`; explicit object deletion uses `remove_node_ids` / `remove_table_ids`. Dangling parents, overlaps and merged-cell cuts still fail. `source_dispositions`, if supplied, replaces the complete disposition list; never blanket-discard unread capture nodes.

## Repair a heading captured as body

If `Symbol definitions` is an actual local heading above several definitions, give it its source-backed heading node and attach those definitions as its body. Remove only the moved ranges from the former owner's body, retaining unrelated surrounding content. For a shared parser block, use exact quote/range selectors; the program computes offsets. Read the regenerated scopes before Items. A standalone heading belongs in structure; a defining or governing sentence remains content. Do not mark substantive text `NON_BUSINESS` to pass a structural check. A table caption follows the different repair below.

## Repair a misplaced pure caption

Pure table titles and repeated continuation captions are structural text. If a previous Map put one in body content and item compilation rejects `structure_only`, retain the table's actual title and exact caption references, and record a source disposition for the pure labels. Do not turn a table caption into a fictitious chapter or extra SRC. For a section whose only body was two pure captions:

```json
{"schema_version":"reqpilot-map-edit/v1",
 "nodes":[{"node_id":"verification-section","content":[],
   "evidence_refs":[{"source":12},{"source":24}]}],
 "source_dispositions":[
   {"source_node_id":12,"disposition":"NON_BUSINESS","reason":"Pure caption retained in the logical table title and exact original reference."},
   {"source_node_id":24,"disposition":"NON_BUSINESS","reason":"Repeated continuation caption retained as an exact original reference to the same logical table."}
 ]}
```

The refs above are invented. Retain the existing `logical_tables` title/fragments and section heading. Each supplied list replaces that whole field: include other existing evidence refs, body content and source dispositions that remain valid. Pure labels stay locatable in original/Map evidence and the table title; they do not need ledger identities. Recompile the local Map, then use its regenerated item requests. If items already exist, use the versioned revision path rather than silently changing their Map. A caption/header carrying an operative condition is not automatically disposable: preserve that condition with its governed content. Never classify source roles from a `Table` prefix alone. `title_source_ids` is for the node's actual heading, not an arbitrary list of different captions attached to it.

## Keep inspection evidence without a second business scope

For text inside a page frame, the frame can remain available for original inspection without becoming another business table. For example, to replace an existing carrier-only table `layout` after confirming its purpose:

```json
{"schema_version":"reqpilot-map-edit/v1",
 "remove_table_ids":["layout"],
 "nodes":[{"node_id":"clause","evidence_refs":[{"source":25,"row_range":[4,6]}]}]}
```

Use actual refs/IDs and original half-open table rows; omit `row_range` for the complete evidence object. References preserve measured locators and a full-source read in the Map review. They do not claim content coverage, change source order or create text/table item requests. Real tables with shared headers and child records still belong in `logical_tables`; this is not permission to remove every overlapping table. A role string alone has no evidence-only effect.

## Recover original text objects missing from normal capture

When context advertises `capture-pdf-text`, call it with the actual `source`, current `source_ir`, selected `page`, optional source-substring `query`, and a new `output_dir`. It appends original PDF drawing-text observations; it is not OCR and does not accept invented replacement text. Original pixels may clip these objects, so inspect the page/crop and retain uncertainty about visibility.

The result contains a new `source_ir` path and ready-to-use refs in `observations`. Use that capture with the saved `map_dir`; previous capture indexes stay valid. A numbered/bulleted observation can use `numbered_body:{source:<new-ref>,label:<exact original prefix>}`. Remove only the old overlapping content selector from its previous owner, retaining any unrelated text sharing that old block. Compilation replays the observation against the original and rejects altered text or locations. It preserves the previous capture and Map; callers do not rebuild IDs, offsets or hashes.

`map-edit-binding.json` records the base Map and actual text bindings; `map-edit-request.json` preserves the edit. Resume from the latest successful directory. Field errors identify their object: `nodes/control/title` and `logical_tables/control-table/title` are different repairs. A whole front-matter block with real content/table evidence may use an explicit display title and empty label; it need not borrow an unrelated source caption. Repair the named fields in one local edit. Do not regenerate the whole JSON, copy read-window wrappers as candidates, or expand the investigation to fix an unrelated field. The native host publishes each successful Map through its existing stage path.

Use `review` (or `review_read` → `map-review.json`) to compare your saved choices with the original. For example, if a target starts at `b)` under an earlier `2.3 Installation`, preserve that real parent; reading the target page and cover does not establish its ancestry. If `b)` really is a top-level clause, keep it there rather than inventing parents. The compiler checks bindings and internal references, not whether your original hierarchy is right. Resolve leads by source inspection; do not reread raw hashes or thousands of unassigned IDs as semantic review. Keep repairs focused on the authorized scope and its actual governing context.

**Cases that change the method:** an original numbered obligation without a separate heading must retain its obligation as content, not turn the entire sentence into title-only context. Table row numbers can be internal data (BOM, complete test), not chapter boundaries. A large page frame may contain several independent tables and clauses. Contents references do not prove attachment correspondence: retain unresolved ownership/numbering instead of guessing the next attachment number. For low-level ranges or reference-only titles, use the existing explicit v2 fields in [Map syntax](tools-map.md); for further reading use the source/table case guides.

For a bounded scope, leave untouched evidence outside the scope uncovered. A page frame containing real content of other sections is not globally `NON_BUSINESS` merely because you selected its text channel for one clause. Do not discard the complete physical capture to silence coverage gaps; later ranges still need it.
