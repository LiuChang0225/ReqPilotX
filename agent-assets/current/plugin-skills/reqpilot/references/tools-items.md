# Source item decisions

Command details for this topic; [tool index](tools.md) provides the common entry.

`compile-items` sorts and deduplicates `reason_codes` as transport metadata; valid codes still use lowercase machine syntax. Source order, boundaries and classifications are not inferred by this normalization.

At the typed `items_apply` / `task items` entry, initial `item_key` and `record_key` may be omitted or null: the program derives them from the resolved source ranges. Supplied keys stay unchanged; existing-scope revisions still preserve returned keys. This entry also accepts lowercase hyphenated reason codes and canonicalizes them to sorted, unique underscore codes. The lower-level compiler still consumes the fully bound form; use the typed entry to avoid assembling its internal metadata.

Read the exact generated scope requests and the current schemas:

```bash
python3 <plugin>/scripts/reqpilot.py schemas --output-dir runtime-data/run/schemas
```

Create `reqpilot-item-decisions/v1` with `source_sha256`, `document_map_artifact_sha256`, `text_decisions` and `table_decisions`. Each text decision contains `scope_id`, `items`, `structure_only_spans` and optional `diagnostic_codes`. Each table decision contains `logical_table_scope_id`, `records`, `context_row_groups` and optional diagnostics. Item/record shapes come from the shared candidate schemas. Omit source/map/request transport hashes inside each scope; the adapter binds them to the selected immutable requests.

Text items cite exact `content_id`, `start_offset`, `end_offset` with half-open Python character offsets. Table records cite existing `fragment_id` and half-open `row_start`/`row_end`. The compiler copies text/cells, preserves shared context and issues stable SRC and REQ. Pure source headings go in `structure_only_spans`, not duplicate SourceItems. Table headers go in `context_row_groups` with role HEADER. Meaningful notes, definitions, legends, introductions and other non-requirements still need SRC records; do not hide them in a context group instead of the ledger. Preserve their applicability/reference links to the complete test or technical object.

`requirementness` is REQUIREMENT or NON_REQUIREMENT. Requirements use sorted unique `requirement_kinds` from FUNCTIONAL, QUALITY, CONSTRAINT, PROJECT, PROCESS and no `non_requirement_role`. Non-requirements have empty kinds and an explicit role from the schema. The first three kinds are under SYSTEM; PROJECT and PROCESS are independent categories. Confidence is a real number, and `decision_summary`/`reason_codes` describe the public source-grounded judgment. These do not replace verification. A label is not automatically a customer number; carry explicit source numbering through the delivery annotation contract when appropriate.

```bash
python3 <plugin>/scripts/reqpilot.py compile-items --map-dir runtime-data/run/map-v1 --input runtime-data/run/item-decisions.json --output-dir runtime-data/run/baseline-v1
```

Unprocessed scopes remain explicit. Outputs stay SLICE_COMPLETE until actual source-driven review proves a larger completion claim; no automatic complete flag is inferred from counts. For incremental extraction supply `--baseline-dir <previous-baseline>` and only new, uncovered scopes. The shared source-order growth compiler preserves existing source records, images, manual judgments, derived/analysis state and display identities while inserting new scopes at their original position. Previously covered scopes are not silently recompiled: use the [explicit revision loop](revision-loop.md) for corrections. Map changes likewise use that loop's source-decisions replay and Map lineage operation.

Text forms include `PARAGRAPH` for ordinary prose, `NUMBERED_CLAUSE` for original
numbered provisions, and `NATIVE_LIST` for an actual list. These are independent
from requirementness/categories and original-number annotations; a paragraph
does not become a list or numbered clause merely to fit a transport schema.

After IDs exist, use `compile-annotations` for original numbers, context links and visual readings; its [input contract](revision-loop.md#produce-a-source-bound-successor) is shared with `review-revise`. A context SRC outside the item's scope is linked by `context_source_ids`, not inserted as a foreign `shared_source_content_id`. Save the sealed annotations into the review, then use [analysis choice and program delivery](tools-analysis-delivery.md). The compiler command exists; no direct internal Python imports or manual artifact hashes are needed.
