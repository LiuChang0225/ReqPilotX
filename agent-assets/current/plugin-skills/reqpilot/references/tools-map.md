# Source-bound Document Map decisions

Command details for this topic; [tool index](tools.md) provides the common entry.

For a new scope or local edit, prefer [incremental Map authoring](tools-map-edit.md) when available: the same `compile-map` command binds source indexes/unique quotes and applies a small change against an existing Map. The explicit v2 format below remains the compiled decision format and low-level fallback.

For new PDF work write an ephemeral `reqpilot-map-decisions/v2` JSON file. Supply source identities, exact offsets where needed and semantic decisions, not rewritten source text or invented coordinates. The adapter builds the existing `DocumentMapArtifactV1`; v1 remains available for unchanged historical replay and native Office.

```json
{
  "schema_version": "reqpilot-map-decisions/v2",
  "source_sha256": "<actual capture source hash>",
  "nodes": [
    {
      "node_id": "main",
      "parent_id": null,
      "kind": "DOCUMENT",
      "label": "",
      "title": "<exact visible title>",
      "title_source_ids": ["<exact SourceIR source_node_id>"],
      "content_source_ids": []
    },
    {
      "node_id": "section-1",
      "parent_id": "main",
      "kind": "SECTION",
      "label": "1",
      "title": "<exact source chapter title>",
      "title_source_ids": ["<heading source_node_id>"],
      "content_source_ids": ["<body source_node_id>"]
    }
  ],
  "logical_tables": [
    {
      "scope_id": "table-1",
      "owner_node_id": "section-1",
      "title": "<evidenced source title, or retained original Map label>",
      "agent_display_label": "<optional explicit short Agent display label>",
      "role": "<observed table role>",
      "fragments": [
        {"source_node_id": "<TableIR source_node_id>", "row_range": [0, 4]}
      ]
    }
  ],
  "source_dispositions": [
    {
      "source_node_id": "<a remaining exact source handle>",
      "disposition": "UNRESOLVED",
      "reason": "<observed source gap>"
    }
  ]
}
```

Empty arrays are valid. `kind` includes DOCUMENT, ATTACHMENT, SECTION and whole COVER/TABLE_OF_CONTENTS/DOCUMENT_CONTROL. A source heading needs verbatim evidence. Untitled DOCUMENT may use the exact filename; whole front matter may use a display title with real content/table evidence and empty `label`. Do not delete a correct role to bypass an error.

If one captured text node contains several real headings/clauses, replace whole-ID entries with `title_source_ranges` and/or `content_source_ranges`, each an array of `{source_node_id,start_offset,end_offset}`. Offsets are zero-based, half-open Unicode character indices into that node's exact `raw_text`. Use native Python to locate unique original markers and compute offsets; do not estimate them or copy source text as a substitute. Ranges cannot overlap. Non-whitespace gaps are reported explicitly. Generated content keeps `source_text_range`; subsequent item spans are local to the generated content, not the original full block. Geometry comes from original line boxes, with an explicitly coarse block fallback; neither claims character-level pixel precision.

For a genuinely untitled body whose label is evidenced only by a contents entry, use `title_reference_source_ids` and real body `content_source_ids/ranges`. These references are stored separately and do not own contents text, establish body pages or affect ordering. Use either direct title evidence or auxiliary references, not both. Do not cite the contents entry as direct body content merely to obtain a title.

Tables belong in `logical_tables`. Fragment `row_range:[lo,hi]` always refers to original physical rows. In v2 generated requests use local `row_count=hi-lo` and cells/row groups starting at zero; original cell IDs, coordinates and original range remain bound. Follow the generated request, never carry original row indices into local row groups. Boundaries cannot cut merged cells; within-row column splitting remains unsupported. The [runnable example](worked-examples.md) demonstrates two clauses inside one capture block.

Each direct source range has one owner; a title reference has no direct ownership. Use explicit PAGE_FURNITURE, DUPLICATE_CAPTURE, NON_BUSINESS or UNRESOLVED dispositions, optionally with start/end offsets for a partial text range in v2. Native text and table capture may overlap: judge from originals, not equal strings alone. Uncovered nodes/ranges remain in the receipt. Ordering uses real positions plus original offsets; unusual columns still require visual review. Unknown v2 fields are rejected rather than silently ignored.

```bash
python3 <plugin>/scripts/reqpilot.py compile-map --source runtime-data/input/source.pdf --source-ir runtime-data/run/capture/source_ir.json --input runtime-data/run/map-decisions.json --output-dir runtime-data/run/map-v1
```

Outputs include `document-map.json`, `table-previews.json`, `scope-requests.json` and the coverage receipt. The existing Map schema's ACCEPTED wire status records a compiler-compatible working Map; `human_acceptance: NOT_CLAIMED` remains explicit. Do not call this engineer acceptance or full-source completion.
