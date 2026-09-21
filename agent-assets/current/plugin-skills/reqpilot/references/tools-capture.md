# Source capture

Command details for this topic; [tool index](tools.md) provides the common entry.

```bash
python3 <plugin>/scripts/reqpilot.py capture --source runtime-data/input/source.pdf --output-dir runtime-data/run/capture --progress
```

This delegates to the application's `fast_source_capture.py`, using the same SourceIR adapters. The output directory must be new. Read the manifest, then the readable views below; keep SourceIR for exact facts rather than routinely scanning all its metadata. Capture may be PARTIAL with explicit gaps. COMPLETE means the native algorithm completed, not that its text covers every visible glyph. Capture uses no OCR or model provider.

For PDF, reuse a matching workspace returned by `reqpilot_context.reading_workspaces`, or prepare one from the same original and capture:

```bash
python3 <plugin>/scripts/reqpilot.py reading-workspace --source runtime-data/run/capture/source.pdf --source-ir runtime-data/run/capture/source_ir.json --output-dir runtime-data/run/reading
```

Native call: `reqpilot_run` with `operation="reading-workspace"` and `source`, `source_ir`, `output_dir`. Use returned paths directly. The page index links full Markdown and 2x renders for every page, including pages with no captured text; table JSON/HTML preserves cells and merges, and image occurrences retain original positions. The source copy, source IR, bookmarks and annotation sidecars remain available. This reuses the captured evidence without reparsing or supplying business decisions.

Read complete MD windows using the returned line counts. Native text may overlap table cells: do not infer duplicate business items. Read full logical-table context before deciding boundaries; HTML is readable markup, while original pixels establish layout and image content. A native-text COMPLETE page can still contain an image-only formula or small nested table.

For visual reading call `reqpilot_read` on the returned original PDF path with `page`; default is 2x. For a detail add `bbox:[x0,y0,x1,y1]` in visible PDF coordinates and `scale` up to 4. Any authorized page/region is eligible, regardless of parser risk flags. If a whole-page zoom exceeds the pixel budget, choose a relevant crop. Base renders omit annotation overlays; inspect the preserved annotation sidecar and use `annotations:true` when checking the marked original. Existing PNG/JPEG assets are also readable.

`reading-packet --source-ir <path> --output-dir <new-dir>` remains an optional complete compact data projection, useful for exact field inspection and non-PDF captures. `inspect-source` is another precise fallback. Older pinned hosts may lack new operations: use their actual catalog. Generated files are not proof of Agent reading; preserve actual checks and unresolved ranges. See [the reading loop](reading-guide.md#bounded-reading-and-repair).

The deterministic StructureTree is an unaccepted navigation candidate. Do not import its technical container hierarchy as the Document Map. Native paint order and a parallel table channel are not final reading order.

Query evidence with `inspect-source --source-ir <capture/source_ir.json>`. With no filter it returns a navigation inventory; `--page 3` or `--source-id <exact-handle>` returns full selected original text, locators and native table cells. These filters are read convenience, never limits on the Agent's source scope. `native_outline.json`, when present, is the original PDF bookmark hierarchy and titles, marked unconfirmed as a Document Map. Also read `pdf-annotations.json` when listed by the capture manifest: PDF comments preserve their own text/type/page/rectangle/xref and metadata. Determine their applicability from this source, separately from body prose; never silently replace the original paragraph, discard an engineering comment because it is not native body text, or assume every comment is a requirement. Record a source-bound gap when the current compiler cannot represent a meaningful comment.

For Map preparation, prefer `inspect-source --view authoring --source-ir <path> --page 3` (when listed by the host). Add `--query <substring>` or `--capture-index <returned-ref>` to focus further. `--offset`/`--limit` paginate nodes without truncating any selected node's text or cell values; follow `next_read`. The view supplies ready Map refs, complete text, actual table fields and full-source read arguments. It does not infer headers, owners or numbering. Native text/table overlap stays visible. The default/raw view remains available for detailed capture geometry.
