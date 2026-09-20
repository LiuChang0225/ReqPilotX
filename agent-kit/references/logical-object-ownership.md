# Logical object and physical-evidence ownership

Load this reference for document structure recovery and extraction verification.
It defines generic boundary tests, never a document-specific layout, filename,
page pattern, or expected object count.

## Pass 1: establish document-unit ownership first

Create a source-ordered ownership ledger before considering table joins:

1. assign every page to one primary unit: main document, one specific
   attachment, or an explicit unresolved boundary;
2. mark visible unit/attachment starts and restarts with exact title, authority,
   numbering, running-mark, and surrounding-page evidence;
3. map figures, assets, and physical tables to that primary unit;
4. derive only the minimum containers required to represent those boundaries.

Multiple separately titled attachments are sibling units even when they discuss
the same product family or repeat the same table schema. Do not replace them
with a catch-all attachment spanning all remaining pages. A section is useful
only when a visible heading or stable scoped role establishes a source boundary;
do not create a section merely because a page begins, a table appears, or a
parser emitted a node. A distinct hierarchical heading with an explicit
section label/number, visible title, and owned heading source range establishes
one section boundary and must not be absorbed into its ancestor. Table and
figure captions, repeated running titles, page headers/footers, and isolated
layout labels are not section headings; keep them with their owning content or
evidence object. Avoid two containers with identical spans and roles.

An evidence-free parent container may organize children when the hierarchy
requires it. It must not become a broad semantic bucket, duplicate child
evidence, or survive as an empty leaf.

## Pass 2: decide each table boundary

A physical table detection is only a bounding/evidence object. It can be a
single logical table, a nested value grid, a false layout detection, or a
detector-coalesced area containing multiple sibling logical regions. Before
considering any cross-page or cross-table continuation, inspect internal title
and header restarts, merged/blank separators, axis changes, record-key restarts,
and applicability changes. The target decision order is:

1. partition one physical detection into evidence-grounded logical regions;
2. classify each region as business, nested, false, or unresolved;
3. only then group compatible regions across physical boundaries as
   continuations.

The current whole-table candidate/proposal contract gives each physical table
one disposition and binds a scope to whole physical table IDs/cells. It cannot
represent two sibling logical regions inside one detection. When this shape is
grounded, mark the current physical table `UNRESOLVED` with a schema-gap reason
and keep the artifact `PARTIAL`; never collapse the regions merely to satisfy
the current schema. Closing this expressiveness gap requires a region-capable
`logical-structure-proposal/v2` and corresponding runner-candidate revision
with exact cell/range/region ownership.

For each supported physical table or region in source order, classify its
relationship to the preceding source-adjacent table/region as one of:

| Decision | Meaning | Runner-schema mapping |
|---|---|---|
| `CONTINUES` | Later physical part continues the same unfinished logical table | both parts use one `logical_table_scope_key` and `LOGICAL_SCOPE_PART` |
| `SEPARATE` | Later table starts an independently scoped logical table | parts use different scope keys and each is `LOGICAL_SCOPE_PART` |
| `NESTED` | Detected table qualifies a specific host cell/record | `NESTED_VALUE_TABLE` with `nested_owner_table_id` |
| `FALSE` | Detection is layout, flow text, or other non-business tabular appearance | `NON_BUSINESS_FALSE_TABLE` |
| `UNRESOLVED` | Available evidence cannot safely select one of the other decisions | `UNRESOLVED`; do not place it in a guessed scope |

The first physical table starts a new scope (`SEPARATE` semantics), is nested or
false, or remains unresolved. Keep the five-way boundary ledger as working
state; serialize only fields allowed by the runner schema.

### Mandatory seven-dimension merge test

Every proposed `CONTINUES` merge must compare both endpoints on all seven
dimensions:

1. **Visible title:** same continuing title, explicit continuation mark, or a
   genuine independent title restart?
2. **Business role:** form, index, planning matrix, test catalog, result log,
   note, or another distinct role?
3. **Row/column axes:** what entities and variables do rows and columns encode,
   beyond raw row/column counts?
4. **Header contract:** labels, grouped headers, order, units, and inherited
   header semantics.
5. **Record schema:** record identity, key fields, conditions, methods,
   criteria, results, and notes expected for one logical record.
6. **Applicability authority:** which attachment, scope statement, configuration,
   component, test series, or normative authority governs the table?
7. **Endpoint continuation evidence:** an unfinished row/record, a carried key or
   merged cell, an explicit continuation label, a repeated inherited header plus
   uninterrupted record sequence, or other evidence that directly connects the
   left ending to the right beginning.

`CONTINUES` requires compatible results for the first six dimensions and
positive evidence for the seventh. Similar topic, proximity, similar numbering,
equal column count, repeated headers, or even an otherwise matching schema is
not enough by itself.

Any positive independence signal—different primary unit, attachment/title
restart, changed business role or axes, incompatible header/record contract, or
changed applicability authority—supports `SEPARATE`. A new attachment or title
restart defaults to `SEPARATE` even when schemas match. Override that default
only when explicit endpoint evidence proves the later part continues the same
unfinished object.

When endpoint evidence is missing or conflicting and neither continuation nor
independence is grounded, use `UNRESOLVED`. Do not infer the answer from an
expected table count or select whichever alternative produces a tidier tree.

## Nested and false detections

A nested table is spatially and semantically owned by a specific host table
cell or record: for example, a tolerance/value grid inside one method cell. It
may point to that validated owner but does not become a sibling logical object.
Its final parent record is decided in the parent-provision workflow.

A false table is a visual layout or flow-text construct with no business-table
record contract. Keep it explicitly disposed instead of dropping it. If visual
and native evidence cannot establish whether a detection is nested, false, or
independent, keep it unresolved.

## Boundary self-audit

Before submission, independently traverse the page and table inventories again:

- one primary unit decision per page;
- one disposition per physical table;
- one five-way decision per source-adjacent table boundary;
- an internal-region check before continuation grouping, with every
  multi-region physical detection left unresolved under the current schema;
- one complete seven-dimension comparison plus endpoint refs for every merge;
- no separate, nested, false, or unresolved table inside a merged scope;
- no title/attachment restart merged on topic or schema similarity alone;
- no duplicated spans, empty leaves, mirrored section/attachment ranges, or
  catch-all attachment container hiding sibling authority;
- every grounded hierarchical numbered heading represented once, with no table
  or figure caption promoted to a standalone section;
- every unresolved boundary visible in the proposal status and reason codes.

The audit is evidence-driven and count-blind. Reproduce the same ownership from
the same source facts; never stabilize a run by adding a target count.

## Cross-stage consistency

The parent-provision workflow may only place a parent family inside a compatible
structure-recovery logical object. If it needs a new object or assigns the same
page/table scope to a different role, return to structure recovery and create a
new artifact version. Do not let a later stage silently correct, reinterpret,
or contradict accepted ownership.

Structure recovery owns logical-table scope and continuation only. The later
workflow owns source-verbatim parent and table-record boundaries. After those
boundaries validate, the Host may pass them into the existing deterministic
table projection and materialization engine; that engine must not invent or
revise Agent boundaries.
