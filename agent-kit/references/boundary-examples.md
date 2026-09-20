# Source-bound boundary decision examples

Load the structure examples during document structure recovery and extraction
verification. Load the parent examples during parent-provision recognition and
verification. These are synthetic shapes, not customer text, filenames, page
rules, or count targets.

## Structure example: main document and multiple attachments

A main specification ends with its approval section. It is followed by an
attachment cover titled `Attachment — Valve Qualification`, then a qualification
table, then another cover titled `Attachment — Connector Qualification` and a
second table. The two attachments use similar terminology and columns.

Assign the main specification and each attachment its own primary page scope.
Create two sibling attachment objects. Do not create one broad attachment from
the first cover through the end, do not merge the two tables by topic/schema,
and do not add a section that duplicates each attachment's full span. If a cover
cannot be assigned reliably, preserve that page boundary as unresolved.

## Structure example: same-schema independent trial tables

Two consecutive tables are titled `Trial A — High Temperature` and `Trial B —
Low Temperature`. Both use `sample / condition / measurement / acceptance` in
the same order and have the same column count. Each title establishes a new
trial identity and applicability scope; neither endpoint contains a carried
row, unfinished record, or continuation mark.

Classify the boundary as `SEPARATE` and create two logical-table scopes. Matching
headers and schema do not override the independent titles and authorities.

## Structure example: multiple logical regions in one physical detection

A detector returns one large physical table because two vertically stacked
trial tables touch the same outer border. Inside it, `Trial Alpha` has its own
header and records; a separator is followed by `Trial Beta`, a restarted header,
and a new applicability statement. They are two sibling logical regions even
though they share one physical table ID.

The target structure first partitions the two exact cell/range regions and only
then evaluates either region for continuation with neighboring evidence. The
current whole-table structure schema cannot encode both sibling regions. Until
a region-capable schema is active, emit the physical table as unresolved and
keep the artifact partial; do not merge the trials, discard one, or pretend the
physical detection is one logical table.

## Structure example: true multi-page continuation

A detailed procedure table reaches a page break in the middle of one numbered
record. The next page says `Detailed procedure (continued)`, repeats the inherited
header, begins with the carried condition/value for that unfinished record, and
then resumes the same record sequence. The title, business role, axes, header
contract, record schema, and applicability authority remain compatible.

Classify the boundary as `CONTINUES`, cite both endpoints, and place both
physical parts in one logical-table scope. Repeated headers or consecutive page
numbers alone would not be sufficient without the endpoint connection.

## Structure example: nested value table

One method cell of a larger trial-record table contains a small temperature by
pressure tolerance grid. Its bounding region is inside the host cell, its labels
qualify only that record, and it has no independent title or applicability
statement.

Classify the detected grid as `NESTED`, bind it to the physical host table, and
do not make it a sibling logical table or a continuation of the next table.

## Structure example: false layout table

A page uses a borderless two-column layout to align an icon with explanatory
flow text. It has no header, axes, records, or independent business role.

Classify the detection as `FALSE` and retain the underlying source text through
the appropriate non-table object or evidence disposition. Do not silently drop
the content merely because the table detection is false.

## Parent example: keep one complete parent

A numbered provision states a shared obligation stem followed by `a)`, `b)`,
and `c)` qualifiers. Keep the provision as one parent when each child depends on
that stem. D4 may project independently verifiable assertions without erasing
the parent.

## Parent example: create multiple record parents

A titled test-results table contains separately numbered records, each with its
own test object, conditions, method, acceptance criterion, and outcome fields.
The title and table are containers. Each complete logical record may be a parent
provision even though all records share a physical table and ancestor heading.

## Parent example: do not repair numbering

If visible records repeat `63`, skip `7`, restart numbering, or mark `4` as
optional, preserve those facts. Do not renumber, deduplicate, insert a missing
record, or infer that an optional record applies.

## Parent example: keep an alternative

If a continued table loses its header and visual/native evidence cannot decide
whether the first row belongs to the previous record, return both grounded
boundary alternatives and the unresolved endpoint. Do not choose the cleaner
count.

## Parent example: separate adjacent but related objects

A matching form is followed by a recommended group/sequence matrix and then a
multi-page detailed procedure table. They share a subject domain but have
different titles, axes, schemas, authority, and record meaning. Keep three
sibling logical objects. Link them with typed applicability or mapping relations;
only the physical parts of the detailed procedure table form `CONTINUES` edges.

## Parent example: keep a nested value table with its record

A test record contains a small tolerance grid inside one method or acceptance
cell. The grid is a nested evidence object owned by that record, not a sibling
test plan or independent requirement merely because the detector emitted a
second physical table.

## Decomposition example: split independently verifiable conjunctions

One parent states several lifetime targets and several environmental limits.
Keep the complete parent, but project separate assertions when each target or
limit can pass or fail independently. Shared subject or grammar is not a reason
to merge distinct evaluation outcomes. Source-range exact-cover verifies that
nothing was dropped; D5 still checks whether the chosen assertions are atomic.
