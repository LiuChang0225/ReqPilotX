# Complete runnable example

For normal cases and special conditions start with [the case index](reading-guide.md). This example is an invented worked answer, not a customer fixture or an automatic semantic extractor.

In a native host run `reqpilot_run` with `operation="worked-example"` and `arguments:{"output_dir":"examples/front-matter"}`. The returned directory contains `map-decisions.json`, `item-decisions.json`, requests and actual outputs readable through `reqpilot_read`; no general shell or script-reading permission is needed. This is synthetic teaching material, never a customer stage result.

In a built bundle the same CLI entry is:

```text
python scripts/reqpilot.py worked-example --output-dir /absolute/new/example-directory
```

In repository development add `--project-root /absolute/ReqPilotX-repository`. The script generates a two-page PDF, capture, v2 Map decisions/requests, items, baseline, Matrix and Excel. Both response clauses deliberately share one native text block: inspect the exact title/body ranges in `map-decisions.json` and the local item spans in generated requests. The source block stays unchanged.

```text
Document
  Cover — whole structure, no SRC
  Contents — whole structure, no SRC
  1.1 Opening response — SRC-0001 / REQ-0001, QUALITY
  1.2 Closing response — SRC-0002 / REQ-0002, QUALITY
  2 Reference BOM — SRC-0003, NON_REQUIREMENT / REFERENCE
    Numbered components remain inside the complete table
```

Expected: three SRCs, two REQs, zero cover/contents SRCs. Each response target keeps its voltage condition. All front-matter words and original BOM cells survive Excel. No internal evidence means analysis is explicitly not run. No Snn is required here because the body obligations already have separate original boundaries.

These counts belong only to this fixture. A normative BOM may be a requirement; a complex unnumbered parent may require authorized Snn; a cover-page obligation needs a body scope. Those changing conditions are covered in [tables](tables-and-relations.md), [source reading](source-reading.md) and [judgment cases](judgment-cases.md). Passing the example proves its program path, not unfamiliar-document/model quality.
