# Requirements-engineering baseline and decomposition boundary

## 2026-08-31 terminology and progressive-ledger override

The generic D3 object is a `SourceItem`, not a `Source Requirement Record`:
a readable source item may be `REQUIREMENT` or retained non-requirement content.
Only the requirement subset is a `RequirementItem` and receives REQ identity.
`SourceRequirementRecord` remains a v1 wire/URL compatibility name. The user
interface says source hierarchy, source item, requirement, retained content,
requirement decomposition, and singular requirement; never expose
`STRUCTURE`, `SOURCE_RECORD`, A/B/F, `SpecObject`, or reason codes as primary UI.

D2 and D3 feed one deterministic progressive ledger. A true one-to-one textual
clause shares one visible row while retaining separate structure/source-item
identities. One-to-many scopes keep the structure row and add source-item
children in exact source order. Pure headings are already represented by the
source hierarchy and must not be duplicated as `NON_REQUIREMENT/HEADING`.
A logical whole table is a structure row; DVP tests, BOM product branches, and
other semantic records are child SourceItems. Human presentation of a table
item must use native headers plus the current cells, shared values, notes, and
images. Markdown/pipe serialization is Agent input only.

Direct text uses the same model. Every selected non-whitespace character is
owned exactly once by a SourceItem or by a structure-only title span, using
zero-based half-open Unicode offsets into immutable source content. Exact-cover
is a fidelity check after the Agent chooses a semantic boundary; it is not
permission for the Host to define items by parser nodes, sentences, tokens, or
transport windows. A node with one textual SourceItem may share one visible row.
If that node also owns child headings or a logical table, its direct-body item
appears as an ordered child before later source children.

The ledger is designed for future ReqIF 1.2 export under mapping profile
`reqpilotx-reqif-1.2-draft/v1`: ordered hierarchy to `SpecHierarchy`, typed rows
to `SpecObject`, attributes to AttributeValue, and actual requirement relations
to `SpecRelation`. Do not claim ReqIF export is implemented until the Host
provides an explicit validated exporter receipt.

Load this reference for D3-D6 decisions. It defines a source-faithful baseline
and an optional transformation layer; it does not prescribe customer-specific
keywords, section numbers, or target counts.

## Authoritative basis

- ISO/IEC/IEEE 29148:2018 defines requirements-engineering processes and
  information items across the life cycle. ISO confirmed the edition in 2024:
  https://www.iso.org/standard/72089.html
- The IREB CPRE glossary defines a requirement as a stakeholder-perceived need,
  a capability/property a system shall have, or a documented representation of
  a need/capability/property. It separately defines context and constraints:
  https://cpre.ireb.org/en/downloads-and-resources/glossary
- NASA distinguishes stakeholder expectations, validated technical requirement
  baselines, and later logical decomposition of parent requirements:
  https://www.nasa.gov/reference/4-0-system-design-processes/
- NASA Appendix C treats one thought and one subject/predicate as qualities of
  a good requirement statement, alongside completeness, traceability,
  necessity, correctness, and verifiability:
  https://www.nasa.gov/reference/system-engineering-handbook-appendix/
- INCOSE's Guide for Writing Requirements describes characteristics and rules
  for well-formed need/requirement statements and sets. Statement-quality
  guidance is not permission to overwrite a source baseline:
  https://portal.incose.org/Web/iCore/Store/StoreLayouts/Item_Detail.aspx?Category=EBOOKS&iProductCode=GUIDEWRITEREQ

## Source Item baseline

First consume the accepted `DocumentMapArtifact`: human-readable titles and
ancestor paths, direct source content, logical tables, continuations, headers,
merged/shared values, notes, figures, accepted revisions, and locators. A new
runtime thread may be clean, but the business workstation must never be blank.

The product object is a `SourceItem`, not an ISO parent requirement. It
preserves one complete source-faithful, independently
reviewable business record without rewriting it. It can be a numbered clause,
a native list, a complete DVP test record/row group, or a BOM product-structure
branch. Physical pages, parser nodes, sentences, cells, arbitrary row counts,
and token windows are evidence or transport boundaries, never semantic record
boundaries.

Textual SourceItems retain exact character spans and verbatim text. Pure titles
remain hierarchy only and are not emitted again as `NON_REQUIREMENT/HEADING`.
Document-control metadata, revision history, explanatory context and other
readable non-requirement content remain visible SourceItems because the final
ledger/export must preserve them. A candidate label is readable source wording,
not an ordinal-only internal boundary key.

The internal baseline classifier uses only A/B/F; UI renders their human labels:

1. A `requirementness`: `REQUIREMENT` or `NON_REQUIREMENT`. Every readable
   record receives a reversible working judgment; `UNRESOLVED` is retry-wire
   compatibility only and cannot be published as A.
2. B `requirement_kind`: one or more of `FUNCTIONAL`, `QUALITY`, and
   `CONSTRAINT`, only when A is `REQUIREMENT`. A compound source record may use
   multiple B values before explicit requirement decomposition.
3. F `non_requirement_role`: `HEADING`, `METADATA`, `REVISION_HISTORY`,
   `CONTEXT`, `DEFINITION`, `RATIONALE`, `EXAMPLE`, `NOTE`, or `REFERENCE`, only
   when A is `NON_REQUIREMENT`.

`OTHER_REQUIREMENT`, `technicality`, and large topic taxonomies are not first
version product axes or completion gates. They may remain historical/internal
compatibility data but must not be exposed as baseline classification.
A normative standard invocation remains a requirement; a bare bibliographic
pointer may be non-requirement reference content.

Do not classify from a modal verb alone. Use the complete source block and ask:

- Does it represent a stakeholder need, required capability/property,
  constraint, interface, quality, performance, acceptance condition, supplier
  duty, process, or deliverable?
- Is applicability established by source authority, a checked option, inherited
  table/header context, or an invoked governing clause?
- Would removing it lose an agreed need, constraint, responsibility,
  deliverable, or evaluable acceptance condition?
- Is it instead descriptive context, metadata, an example, rationale, plan
  input, blank template field, heading, or non-normative reference?

Words such as `shall`, `应`, and `必须` are evidence, not the ontology. Tables,
lists, checked boxes, acceptance columns, and shared heading stems can express
requirements without repeating a modal verb in every row. Conversely, a modal
sentence can be a quotation, example, heading, or reference context.

Every suggestion carries a concise human-readable decision summary, bounded
internal reason codes, exact bundle-local evidence, and confidence. Reason
codes are audit diagnostics, not evidence and not default UI copy. When
authority or applicability is weak, preserve that fact through evidence state,
low confidence, and grounded uncertainty reasons rather than withholding A.
A genuinely missing or unreadable source enters object-local evidence recovery
without blocking readable peers. The user may override any suggestion before
or after accepting the working baseline.

For tables, the Agent owns business row groups or product branches. The Host
may validate exact cell partition and then invoke a deterministic materializer
for headers, merges, shared values, notes, images, stable IDs, and PDF locators.
The materializer's first candidate is advisory: the Agent may submit a complete
replacement when source or rendered evidence shows it is wrong.

Classification must use the complete multimodal row. An empty OCR/native cell
does not make a numbered row non-normative when a drawing, coordinate plot,
nested table, symbol, or screenshot in that same row carries its payload. Open
the rendered source before deciding; preserve visual evidence without inventing
an unavailable transcription. Visual evidence applies only when its placement or
explicit source binding belongs to that row; do not borrow a nearby nested table
to fill another row's empty acceptance cell.

A DVP record can impose a test, measurement, report, curve, or reference-output
obligation without defining a product quality threshold. Apply classification
V1 to the actual obligation: project deliverables/milestones are PROJECT,
activities/responsibilities/approvals are PROCESS, and an explicit restriction
of product design choices is CONSTRAINT. The mere presence of a method,
standard, numeric condition or report does not imply CONSTRAINT. Verification
equipment, methods, parameters and criteria remain linked to their technical
review. Use QUALITY for a required product characteristic, target, limit or
acceptance outcome. An empty acceptance cell or "for reference" wording does
not invent a quality target. Topics remain separate from these categories.

For technical verification, interpret its complete purpose, product target,
equipment, methods, conditions and criteria together (classification V1/R06).
Describing a test activity does not by itself make it an organizational PROCESS
requirement. A table-introduction pointer or a notice that no criterion is yet
specified can be retained connected source context, without creating a duplicate
obligation. A normative TBD parameter cell differs: retain the underlying
product property/category and unresolved criterion, without a fabricated limit.

A deferred value is not automatically non-requirement. In a normative table,
statements such as a parameter being TBD, tuned later, or released after a
named activity can still reserve a mandatory property, future controlling
baseline, or deliverable. Keep that SourceItem as a requirement when removing
it would lose the obligation/property, and express uncertainty through
confidence and bounded reason codes. Use `NOTE/CONTEXT` only when the source is
genuinely explanatory scheduling information with no required subject,
property, responsibility, or future controlling value.

## Explicit requirement decomposition

Requirement decomposition begins only after a source-faithful baseline exists
and a user explicitly selects a requirement scope. It is performed by the
dedicated decomposition Agent, not as a hidden continuation of extraction.

- Preserve the exact parent as the baseline and evidence owner.
- Treat singular children as a derived semantic projection. Unless a child is an
  exact source span, label it transformed/normalized rather than verbatim.
- Use one independently verifiable thought per child while retaining shared
  conditions, exceptions, units, and parent lineage.
- Show before/after, source ranges, impact, and acceptance status. Rejecting or
  undoing decomposition restores use of the unchanged source item.
- Requirements analysis consumes the accepted choice: approved singular
  children for a decomposed scope, or the unchanged requirement item when the
  user keeps it whole. Starting analysis never triggers decomposition implicitly.
