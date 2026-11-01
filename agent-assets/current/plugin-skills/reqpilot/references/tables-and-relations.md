# Tables and relationships

Use when a source contains a table or table-like layout. Recover its real owner and logical extent before choosing item granularity. Preserve cells/merges/notes/images even when they are not independent items. Cases below are synthetic abstractions of prior document reviews.

## Choose the business unit

| Case and source shape | Normal result | Distinguishing evidence / countercase |
|---|---|---|
| T01 Parameter table: `Idle current \| at rated voltage \| max 0.8 mA`; another row specifies startup time | Separate complete property records where each row independently states a requirement. Include condition, min/nominal/max and unit together | Several operating points/tolerances of one target remain a parameter set; do not split every value into a requirement. Retain the governing provision without double-counting its detailed obligations |
| T02 BOM: numbered housing, seal and fasteners form one assembly | One complete analyzable branch; preserve part hierarchy, quantities, versions and status internally | Independently complete product branches may be separate items. A normative supply/configuration statement can make the branch a requirement; the name BOM never means automatically non-requirement |
| T03 DVP: one test number spans setup, exposure and post-test inspection | One full test record, including method, criteria, sample/phase, responsibility, duration, notes and figures; it may span rows/pages | Distinct complete test numbers normally form separate records. Independent response outcomes may become Snn later; steps and sample quantities are not orphan product requirements |
| T04 Message layout, enum list or applicability matrix | Keep the defining statement with structured byte/bit/value or scenario content | Byte offsets are not clause numbers. Independently specified services/behaviors may need separate source items; cell intersections become separate test scenarios only when that later task is authorized |
| T05 Schedule or product information table | Preserve the whole business organization and judge its actual obligations | A descriptive schedule/configuration is not automatically a requirement. Binding delivery dates can be PROJECT, imposed configuration CONSTRAINT; do not classify every row as product QUALITY or every information table as background |

For T02, identify what the source says must be supplied or assessed together. `One valve assembly comprises: 1 housing, 2 seal, 3 fasteners` is one BOM unit, even when each row has its own part number, quantity or manufacturing note. Preserve those original ordinals in the unit; row numbers alone do not justify separate requirements. Calling each flat row a “branch” does not establish an independent BOM.

If the introducing sentence is source-owned by this BOM, bind its exact text span and the table rows to the same table record. Mirror the text range in its text scope as table-owned; a shared-content reference alone does not prevent a duplicate SRC/REQ. A separately numbered obligation beside the table retains its own text item.

Countercase: the source explicitly gives separate `Variant A supply list` and `Variant B supply list`, each with its own complete component set and applicability. Those can be separate BOM units even when printed in one physical table. Cite that source boundary when separating them; different component names alone are not that boundary. Preserve a real shared condition without linking unrelated variants.

## Preserve relationships across boundaries

| Case | Handling and change condition |
|---|---|
| T06 Multi-level headers, units and vertically merged values | Build full header paths and reference each shared value for its actual covered records. A blank covered by a merge is not a missing value. Geometry is strong scope evidence, but conflicting wording/explicit exceptions require adjudication; do not universally copy values down or let a parser merge override source semantics |
| T07 Footnotes and trailing prose | Follow the marker and stated scope. A whole-test calculation rule stays shared; a note marked only for item B does not apply to A. Independent normative text after the table remains its own source range. “At the bottom” does not establish universal applicability |
| T08 Cross-page continuation | Compare both endpoints: title, column meanings, units, repeated header, numbering and unfinished record. Proven continuations form one logical table and one complete cross-page record. A new attachment, changed axes or a new test block can break continuation despite identical columns |
| T09 Whole document inside a page frame | Treat the outer grid as layout evidence. Recover the actual internal sections, direct prose and content tables; do not make the whole page one requirement or a fictitious table chapter. An unsupported intra-cell split stays an explicit representation gap |
| T10 Nested table/image inside a record | Retain it at its owning cell/record with shared context; it does not automatically become a standalone IMAGE SRC. A nearby figure outside the table needs independent ownership evidence. Tool rectangles and whole-page screenshots are not exact cell bindings |
| T11 Checkboxes, option sets and blanks | Preserve question, every option and checked/unchecked/uncertain states. A selected market may define applicability; a selected risk flag may only be metadata. Unchecked content remains visible but inactive when source semantics establish a choice. Plain lists may require all entries. Empty, `/`, `0`, `N/A`, dash and merged continuation are distinct; use explicit placeholder declarations for actual empty templates |
| T12 Long-table batches | Split tool reading into manageable complete records with their headers, conditions and references. Integrate through stable record/table identities and global source order. Batch/page/token limits never create new business tables or duplicate headers/requirements |

After choosing the shape, check every original cell and note has the correct owner or explicit context role. An all-context cover/control table can have zero SRC while remaining readable. A body table cannot be relabelled as front matter to avoid judgment. A header containing a genuine common obligation still needs an explicit semantic disposition.
