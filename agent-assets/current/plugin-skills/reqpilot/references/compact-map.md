# Read source, then express its structure

Use the actual capture's short `ref` values. The example below illustrates shape only; replace every `null` with an actual source selection. Do not query every schema definition before reading the document.

```json
{
  "outline": [{
    "role": "DOCUMENT",
    "children": [{
      "role": "HEADING",
      "ref": null,
      "content": [],
      "children": [{"role": "CLAUSE", "ref": null}]
    }]
  }]
}
```

Pass this as `map_apply.decisions` with the current task/revision. A virtual DOCUMENT root uses the actual source filename. A source-backed document or attachment uses its own `ref`; independent documents may be separate roots. HEADING is a real source heading; CLAUSE is a numbered original body provision, whose full wording stays body. Ordinary unheaded paragraphs belong in their actual parent's `content`. Nesting expresses your chosen hierarchy; numbering does not infer parents.

`ref` accepts a capture index, full source ID, or the existing exact selector such as `{"source": 7, "quote": "<unique complete source clause>"}`. Unambiguous heading/clause number prefixes are copied from the original. An explicit `label` overrides that choice only when it exactly matches the source. Keep continuations, figures and captions in `content` of their true owner. Preserve image interpretation for the later annotation step.

Attach complete logical tables to their owner:

```json
{"sources": [17, 24], "title": "<actual table title or established label>", "role": "<source-based table purpose>"}
```

This object goes in that node's `tables`. Include all continuation fragments; partial original rows use `{"source":17,"row_range":[0,3]}`. The numbers are illustrative. The program computes scope IDs and parent/owner references; you still decide continuation and grouping. BOM component numbers do not force separate requirements.

COVER/CONTENTS/CONTROL are whole source-backed structural blocks. They may cite a real title in `ref`; without a separate heading, cite their complete content and let the structural role supply the display label. Do not turn a metadata field into a fabricated heading. `evidence_refs` retains the existing source/row_range reference form when evidence supports a node without becoming its owned content.

`outline` and nonempty `nodes/logical_tables` are alternative inputs for one call. Stable IDs depend on actual source positions, not traversal indexes or parents. Omitting an existing object does not delete it; precise revision/removal uses the existing node IDs and explicit removal fields. Coverage, conflicts, source fidelity, save/replay and workbench publication still use the same compiler. Unselected evidence is never automatically discarded as noise; supply actual `source_dispositions` when justified.
