---
name: reqpilotx-parent-provision-recognition
description: "Build a source-faithful Source Item baseline from an accepted document hierarchy. Decide business item boundaries and A/B/F without decomposing requirements or following parser and physical-window partitions."
---

# Source Item baseline

Produce the progressive Source Item ledger used by ReqPilotX before optional
requirement decomposition. `SourceRequirementRecord` and `parent provision` are
internal compatibility names, not ISO parent requirements.

Use this Skill only for the execution mode named by the Host:

- `SOURCE_REQUIREMENT_SLICE`: classify one logical table and return the declared
  table-slice candidate.
- `SOURCE_REQUIREMENT_BOUNDARY_PLAN`: decide the complete business records and
  exact row/context partition for one logical table, without A/B/F.
- `SOURCE_REQUIREMENT_CLASSIFICATION_WINDOW`: classify only the Host-frozen
  records listed in one transport window; never merge, split, relabel or reorder
  those boundaries.
- `SOURCE_ITEM_TEXT_BATCH`: classify the selected direct-text scopes and return
  the declared text-batch candidate.
- `SOURCE_ITEM_TEXT_BOUNDARY_PLAN`: decide exact source-faithful record spans and
  structure-only title spans for exactly one direct-text scope, without A/B/F
  and without inventing a machine key.
- `SOURCE_ITEM_TEXT_CLASSIFICATION_WINDOW`: classify only the Host-frozen whole
  direct-text records listed in one transport window with the canonical A/B/F
  decision; never change their spans, labels, forms, shared sources, order, or
  Host-issued keys.

All declared transports grow the same ledger. Stop after the requested candidate; do
not run historical parent-provision extraction or requirement decomposition.

## Authority

The Agent owns business boundaries, source form, requirement judgment and the
human-readable explanation. The Host owns source identity, hashes, exact
row/cell or character coverage, stable IDs, persistence and publication.
Parser, resolver, OCR and table outputs are evidence tools, not mandatory
semantic boundaries.

For compact table execution the Host may supply `document-context.json` instead
of the complete Document Map. It is a validated projection of the current node,
ancestor path, adjacent logical scopes and document summary; `scope.json`
contains the immutable selectable evidence. Do not request the full Map merely
to repeat identity or unrelated nodes.

Compact input is a default working set, not a permission boundary. The complete
authorized Map, exact scope geometry and original document remain read-only and
available on demand. Inspect them when definitions, applicability, references,
continuations, numbering anomalies, visual placement, merged cells or conflicting
context cannot be resolved locally. Global context may inform the decision, but
the returned row groups or text spans must stay inside the selected scope. Request
adjacent coordination or a localized Map revision instead of publishing across
that boundary.

When the rules below do not resolve an A/B/F distinction, source-record boundary
or decomposition boundary, read the [requirements-engineering baseline](../../../references/requirements-engineering-baseline.md).
Do not load that reference merely to restate an already clear numbered record.

## Business records

- Choose independently reviewable source items from meaning: complete numbered
  clauses, DVP tests, BOM product branches, interfaces or equivalent business
  records.
- Do not split on page, parser object, token window, physical row, sentence
  punctuation or conjunction alone. Do not target a remembered item count.
- In a DVP, one numbered test plus its merged and continued rows is normally one
  `TABLE_RECORD`, including cross-page continuations.
- In a BOM or component hierarchy, use `TABLE_BRANCH` for product-structure
  branches and keep component rows inside the branch instead of creating one
  requirement per part.
- Assign every selected table row exactly once to one record or one typed
  context group. Repeated/shared headers are `HEADER` context. Never duplicate
  a row.
- For direct text, assign every non-whitespace character exactly once to one
  Source Item span or one `structure_only_span`. A pure visible heading is
  structure-only; metadata, context, definitions, notes and references remain
  classified Source Items.
- `shared_source_content_ids` may add source content owned elsewhere that applies
  to this item. Never repeat a content ID already owned by this item's
  `text_spans` as shared evidence.
- Multiple fragments or text blocks belong to one item only when they are a
  physical continuation or one complete business record.
- A table business record may directly own exact body-text spans that form one
  complete provision with its table. Express those as `owning_text_spans`; they
  are distinct from shared references. Do not also emit an independent text
  item owning the same characters.
- When `table-ownership-context.json` is present, its `table_owned_spans` are
  Host-reserved ownership already accepted from table stages. Return that exact
  reservation unchanged in the text boundary plan, and partition only the
  remaining text. Never omit, extend or relabel a reservation to satisfy coverage.
  Without that Host context, `table_owned_spans` must be empty.
- Do not hide a table preamble as context merely because it also contains a
  purpose, scope or explanation label. If the same row prescribes a default
  standard, conditional deliverable, approval input or other independently
  auditable obligation, preserve the complete row as a Source Item. Only pure
  titles, column headers and structural separators may be context. Business-
  meaningful metadata, examples, form values, notes and illustrations remain
  non-requirement Source Items with the appropriate F role, even when the form
  has not yet been filled for a specific product.

Boundary planning must close the complete logical table before classification
windows are created. Windows are transport after semantics: each one contains
whole frozen records and shared context, may inspect the complete read-only
Boundary Plan and document evidence, and returns exactly one decision per Host
record key. A failed window may be retried independently; accepted sibling
windows must not be regenerated. Classification uncertainty changes confidence
or reasons, not the frozen boundary. A genuine boundary defect returns to the
Boundary Plan rather than being repaired inside a classification window.

Direct text follows the same two-stage rule, but its semantic unit is never
chosen from sentence punctuation, parser nodes, or a character budget. Finish
and Host-validate each requested scope's Unicode exact-cover before any text
classification window is formed. Character load and risk may schedule frozen
whole records only after those semantic boundaries exist. A failed scope or
classification window may be retried independently; accepted siblings are
immutable checkpoint inputs and must not be regenerated.

When `revision-guidance.json` is present, this is an affected-only replacement:

- Guidance items may be user feedback or a neutral Host review request when no
  user feedback exists. Treat either as a question to evaluate against the
  source, not as source evidence or permission to use other tools.
- `current_affected_scopes` is the accepted starting boundary, not new source
  evidence. Verify it against the immutable scope and guidance; preserve it when
  it remains source-supported, and change it only for a source-grounded reason.
- Read declared comparison scopes only to apply a consistent professional
  boundary principle; never return or modify them.
- Return a complete replacement for every affected scope, including unchanged
  evidence that should remain. Do not emit a patch, deletion command or partial
  span list.
- If feedback conflicts with the source, preserve the source-supported result
  and explain that judgment in the ordinary decision summaries.

## A/B/F judgment

Every Source Item receives a reversible working judgment:

- A=`REQUIREMENT`: B is a non-empty sorted subset of `FUNCTIONAL`, `QUALITY`,
  `CONSTRAINT`, `PROJECT`, `PROCESS`; F is null. The first three belong to
  product/system requirements; PROJECT and PROCESS are independent categories.
- A=`NON_REQUIREMENT`: B is empty; F is one declared non-requirement role.

Never emit `OTHER_REQUIREMENT`, technicality, final `UNRESOLVED`, rewritten
text or singular children. Lower confidence and explain uncertainty when the
best source-supported judgment is not certain.

Use the current classification V1: product behavior/capability is FUNCTIONAL;
performance and quality under its conditions is QUALITY; an explicit restriction
of product design/implementation choices is CONSTRAINT. Project deliverables and
milestones are PROJECT; organizational activities, responsibility, approval and
execution rules are PROCESS. Topics, standards references and verification
relations are independent. Do not turn a test method, equipment, measurement,
report, curve or standard reference automatically into a product CONSTRAINT.
Retain equipment, method, parameters, conditions and criteria together with the
technical review they support. A blank acceptance cell or
"for reference" does not import a threshold from another row. A deferred value
such as `TBD`, `待定` or `后续释放` remains a requirement when it reserves a
required property, supplier deliverable or future controlling baseline.

Classify a named drawing/specification by the actual obligation and available
source; its mere title does not create a product QUALITY or CONSTRAINT claim.
Unobtained standards remain explicit references, not invented requirements.
Classify duties to annotate, identify or submit information according to the
actual project deliverable or process responsibility. Mixed parent items may
carry multiple categories without being mechanically split.

### Technical verification context (classification V1 / confirmed R06)

Read the complete owning section and original logical table, then identify the
product function or quality being verified before assigning categories. Keep
the equipment, method, samples, conditions and criterion linked to that target.
A test activity or an instruction to use particular test conditions is not an
organizational PROCESS requirement merely because it describes an action.
PROCESS concerns an independently required organizational/execution workflow,
responsibility or approval regime; do not route the product's verification
method/conditions away from technical assessment into management-process work.

A pointer such as “use the following conditions” may introduce the table that
carries the complete verification provision. Preserve it as connected source
context rather than manufacture a duplicate independent obligation. A statement
that a criterion has not been specified is retained gap/context information;
it does not itself add another future obligation. Distinguish that statement
from a normative parameter cell that reserves a required product property with
an as-yet undefined acceptance value. Keep its underlying product category and
undefined status without inventing a threshold or a new PROCESS category.

Frozen transport windows are not semantic walls. Read `semantic_context` and
its complete related original tables; a row or introductory sentence alone is
insufficient evidence for its final requirement/verification role.

A purpose, objective or rationale phrase does not add `QUALITY` by itself. When
one indivisible source row mixes such context with a binding standard or
deliverable, keep the whole row as the Source Item but classify B from the
normative content unless the source also states a binding product
characteristic or acceptance result.

## Evidence fidelity

Preserve source text and cells. Use a concise source-visible label and a short
Chinese `decision_summary` explaining the business boundary and normative
basis. Keep reason codes unique, sorted and lowercase snake_case.

When a row contains an image, drawing, plotted value or nested visual table, or
extracted text is empty, inspect the authorized rendered page before judging
it. Preserve unreadable visual evidence without guessing values. Do not apply a
nearby image to a row unless overlap or explicit source placement proves the
relationship.

The visible product presentation is a Host postcondition: whole-table
selection renders the complete native table; Source Item selection renders
inherited headers plus current rows, shared values, notes and images. Never
flatten tables into Markdown for the user.

If evidence proves the accepted structure is wrong, propose a localized
Document Map revision rather than shrinking the correct business record to fit
an obsolete boundary. A Host validation failure is bounded feedback about the
same immutable scope; correct the schema, range, overlap or coverage defect and
return one complete replacement.
