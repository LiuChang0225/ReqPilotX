---
name: reqpilotx-logical-structure-reconstruction
description: Reconstruct source-bound document units, attachments, logical tables, physical continuations, and complete evidence disposition from validated page evidence. Use during document structure recovery before deciding parent or table-record provisions.
---

# Logical structure reconstruction

During **document structure recovery**, return a
`D2LogicalStructureDecisionCandidateV3`
(`d2-logical-structure-decision-candidate/v3`). The Host validates it and
compiles the durable `D2LogicalStructureProposalV2`
(`logical-structure-proposal/v2`) from validated document-understanding
evidence. Load the [core
ontology](../../../references/core-ontology.md), [evidence
authority](../../../references/evidence-authority.md), [boundary
examples](../../../references/boundary-examples.md), and [stage
contracts](../../../references/stage-contracts.md). Before grouping physical
parts, read [logical object ownership](../../../references/logical-object-ownership.md).
Also read [Agent-native Document Map
recovery](../../../references/document-map-recovery.md). This work continues the
same primary Agent's whole-document mission: the accepted survey and prior scope
checkpoints are working context, while the complete Evidence Workspace remains
available whenever the current hypothesis needs to be tested.
Use the attached, provenance-locked OpenAI `$pdf` capability to choose native
and visual inspection methods. This is a read-only ReqPilotX run, so do not use
its authoring, form-filling, dependency-installation, output-directory, or final
file-citation instructions. This Skill remains the sole authority for hierarchy,
content ownership, logical tables, figures, and attachments.

## Stable two-pass protocol

Run both passes in order. Do not start from an expected object or table count,
and do not let a parser/resolver grouping substitute for either pass.

### Pass 1: document-unit, attachment, and page ownership

1. Validate the locked product Evidence Workspace. Read its manifest, page
   index, source map, table index, asset placements, diagnostics, and contact
   sheet before proposing structure. Use full page/table files and scoped visual
   inspection as needed. Read `pages/source-ordinal-ledger.jsonl`; its zero-based
   `source_ordinal` defines evidence order and is not the parser's
   `source_order`. Do not turn file order into a tool-call checklist. A Host
   heading skeleton or survey scope is navigation evidence, not a permission
   boundary: inspect adjacent or earlier pages whenever they can confirm
   attachment ownership, hierarchy, continuation, or a contradiction.
2. Bind the exact validated document-understanding artifact, Kit identity,
   SourceIR canonical SHA, and table-map artifact before proposing any object.
3. Build a source-ordered scratch ownership ledger that assigns every page to
   one primary document unit: the main document, one specific attachment, or an
   explicit unresolved unit boundary. Detect attachment starts and restarts from
   visible titles, scope/authority statements, numbering resets, running marks,
   and surrounding page evidence. Similar subject matter is not ownership
   evidence.
4. From that ledger, inventory the minimum evidence-bearing document,
   attachment, section, provision-scope, figure, context, and table-region
   candidates. Declare only non-table objects in `logical_objects`; Pass 2 owns
   all table region and logical-scope declarations.
   Make separately titled or independently authoritative attachments siblings;
   do not wrap them in one broad, ambiguous attachment object. Add a section
   only when a visible heading or stable scoped role establishes a real
   boundary. Every distinct hierarchical heading with a grounded explicit
   section label/number, visible title, and owned heading source range must have
   exactly one section boundary; do not collapse child headings into their
   ancestor merely to make the graph smaller. Conversely, a table caption,
   figure caption, repeated running title, page header/footer, or isolated
   layout label is not a section heading. Keep a table/figure caption with its
   owning section or evidence object and let Pass 2 own the table region. Do not
   mirror the same span as both a section and an attachment, create one section
   per page/table, or add placeholder containers.
5. Prefer compact half-open `source_spans` between exact SourceNode endpoint
   handles in the Host-pinned ordinal ledger; the Host expands every intervening
   non-table SourceIR node into exact final source segments. Use `source_ranges`
   only for a truly discontinuous or partial handle boundary. For a whole
   text-bearing handle, set both offsets to null; the Host resolves this
   deterministically to `0..len(source_text)`. Copy current endpoint handles;
   never reconstruct them from memory.
6. A document, section, attachment, or context container may have no direct
   range when its children own all evidence. Do not duplicate child ranges on
   the parent. The Host derives an empty container's pages, order, and stable ID
   from child evidence plus its parent identity. An empty container with no
   child evidence is a no-op and the Host removes it.

### Pass 2: physical-table disposition and adjacent boundaries

1. Traverse every physical table in source order. Query its cells, merges,
   visible title, headers, notes, images, bounding region, leading/trailing
   context, and owning document unit. First inspect internal title/header resets,
   axis changes, blank separators, and applicability changes to decide whether
   the physical detection contains zero, one, or multiple logical regions. A
   physical detector box is evidence, not proof of one logical object.
2. Partition every non-empty physical table into exact, non-overlapping cell
   ordinal regions before grouping continuations. The V3 runner schema can
   represent multiple sibling logical scopes, ordinary table-framed content,
   nested values, false/layout regions, and unresolved regions inside the same
   physical table. Do not collapse a known multi-region table into one scope or
   mark it unresolved merely because an older whole-table schema could not
   express the partition.
3. For the first supported table/region and every subsequent source-adjacent
   boundary, make exactly one working decision:
   `CONTINUES`, `SEPARATE`, `NESTED`, `FALSE`, or `UNRESOLVED`.
4. Before every `CONTINUES` decision, compare all seven dimensions on both
   endpoints: visible title, business role, row/column axes, header contract,
   record schema, applicability authority, and explicit endpoint-continuation
   evidence. The comparison and source refs must support the merge; equal column
   count, similar wording, proximity, shared topic, or matching schema alone do
   not.
5. Default a new attachment, attachment restart, or independent visible-title
   restart to `SEPARATE`, even when its schema matches the prior table. Override
   that default only when explicit endpoint evidence proves the later physical
   table continues the earlier unfinished logical object. A cross-page location
   by itself is not endpoint evidence.
6. Use `NESTED` when a detected table is contained by and qualifies a specific
   host table cell/record; use `FALSE` for layout, flow-text, or other
   non-business table detections. If the evidence cannot distinguish two viable
   boundaries after both endpoints are inspected, preserve the ambiguity as
   `UNRESOLVED`. When one option can be expressed as a Host-validated,
   reversible presentation proposal, use the first source-grounded proposal as
   the default working view while keeping the unresolved marker and alternatives
   visible. Never turn that default into source truth or pick an option merely
   because it produces a cleaner count.
7. Map the working decisions to V3 region fields. `CONTINUES` regions share one
   `logical_scope_key` and appear in one ordered `logical_scopes` declaration;
   `SEPARATE` regions use distinct scope keys. Emit `NESTED_VALUE_REGION` with
   its validated owner region key, `NON_BUSINESS_FALSE_REGION` for false/layout
   cells, and `UNRESOLVED` only for the exact undecidable cells. Use
   `LOGICAL_OBJECT_PART` for ordinary source content captured inside a table
   frame and map each such region-object key exactly once through
   `region_logical_object_placements` to a declared non-table `logical_objects`
   target. Place every logical scope through
   `region_logical_scope_placements`. Do not serialize the scratch boundary
   ledger outside fields permitted by the output schema.
8. Use parser facts and resolver suggestions only as candidates. Inspect both
   endpoints before accepting a continuation. Semantically related but separate
   objects may be connected later by a typed relation such as `MAPS_TO`,
   `QUALIFIES`, or `USES_PROCEDURE`; semantic linkage is not continuation.

## Evidence coverage and output

1. Give every TableIR cell anchor exactly one region disposition in source
   order: `LOGICAL_SCOPE_PART`, `LOGICAL_OBJECT_PART`,
   `NESTED_VALUE_REGION`, `NON_BUSINESS_FALSE_REGION`, or `UNRESOLVED`.
   Region `cell_spans` are half-open ranges over the Host-published canonical
   cell ordinal order. They must exactly partition each non-empty physical
   table without overlap or gaps. Declare a zero-cell TableIR explicitly as
   false/layout or unresolved through `empty_table_dispositions`; never invent
   a cell region for it.
2. Bind every physical table exactly once. Also disposition every non-table
   SourceIR range or asset exactly once as logical-object evidence,
   non-business source, page furniture, nested evidence, or unresolved. This is
   an evidence coverage ledger, not a parser-defined requirement universe. The
   Host derives `LOGICAL_OBJECT_PART` dispositions directly from each object's
   selected source ranges; do not repeat them. Emit `source_evidence_dispositions`
   only for source that is not owned by an object: non-business source, page
   furniture, or unresolved, using the source handle and exact range directly.
   If a source range cannot be decided, emit `UNRESOLVED`; never omit it or
   guess `NON_BUSINESS_SOURCE`. The Host deterministically completes any
   accidentally uncovered range as `UNRESOLVED`, makes the artifact `PARTIAL`,
   and exposes that gap in the validation receipt instead of silently losing
   source content or blocking the whole document.
3. Treat table-node raw text, table notes, and visual table evidence as
   supplemental evidence outside the cell-anchor partition. For every present
   supplemental kind, emit at most one explicit placement: `CELL_DERIVED` only
   when the Host can prove its exact canonical cell-text derivation,
   `OWNED_BY_OBJECT`, `OWNED_BY_SCOPE`, `NESTED`, `NON_BUSINESS`, or
   `UNRESOLVED`. Do not emit `ABSENT`; only the Host can derive absence. If
   present supplemental evidence is omitted, the Host preserves it as
   `UNRESOLVED` and makes the proposal `PARTIAL` rather than dropping it.
4. Use sorted lowercase snake_case reason codes and table-bound evidence refs.
   Leave table-record and parent boundaries to the later parent-provision
   workflow, where the existing deterministic table materializer can consume an
   accepted parent boundary.

## Boundary self-audit before submission

### Targeted failure-pattern lookup

Do not load an accumulating counterexample catalog into the stage prompt. When
Host diagnostics expose one or more observed failure signals, select at most
three relevant reminders with the native dynamic tool
`reqpilotx_structure_select_failure_patterns` when it is available. Otherwise
use `scripts/select_structure_failure_patterns.py --signal <signal>`. Do not
read the full catalog when no signal is present. Pattern reminders are advisory
questions; current source evidence remains authoritative. The dynamic tool is
optional and Host-executed; do not call both routes for the same signals.

Use these signal families: confirmed action without materialization or
verification; visual subcell scope leakage; parent/child evidence duplication;
repeated title extraction; continuation column topology drift; undispositioned
cross-cell visual placement; and non-navigable inline-list oversegmentation.
Never infer a signal merely because a past document had that failure.

User confirmation accepts a direction only. It never proves that a proposal
was materialized, reopened in the rendered view, or independently verified.

Choose Host tools by expected information gain, not as a fixed checklist. If
the current source and rendered evidence already decide the boundary, proceed
without an extra tool call. Call a native-text, render, table, ownership, or
validation tool only when its result can resolve a current signal, produce a
needed materialization candidate, or test a postcondition. Record the tool
intent and current signal; tool-call count is never a completion target.

Treat tool results according to their authority. Source evidence is immutable
and can only be cited. A parser, resolver, table-restoration, or other advisory
candidate may be wrong: compare it with source evidence, then emit a complete
replacement candidate when cells, ownership, columns, visual placement, or
navigation granularity need correction. Bind the replacement to the tool-result
SHA and evidence refs; never edit the raw source or claim that the tool itself
returned the corrected result.

When the Host binds this turn to an acknowledged structure handoff and exposes
`reqpilotx_structure_get_handoff_context`, call that context tool exactly once
before deciding the change. Use `reqpilotx_structure_inspect_scope` only when
the returned source content, anchors, tables, and user direction cannot decide
the current scope. Do not turn inspection into a fixed tool checklist.

When the same turn exposes `reqpilotx_structure_publish_revision`, call it only
after the complete replacement candidate is ready. Supply target scope, basis,
operations, evidence/tool refs, proposed work-item dispositions, and public
action/summary text. The Host computes revised-candidate hashes and stable
revision IDs; do not calculate or echo them. Also do not echo source, artifact,
handoff, attempt, command, or persistence identities because the Host injects
them. A successful call proves only that an immutable revision and public
Activity were persisted and materialized. It must return
`VERIFICATION_REQUIRED` until the real rendered scope is reopened and Host
validation passes. Do not call the publisher when no candidate changed, do not
publish partial operations as separate micro-updates, and never treat the tool
receipt as source evidence or final verification.

User confirmation is not required before selecting a working projection. If
ambiguity remains but at least one source-grounded proposal is supported by the
available Host operations, publish the first Host-valid reversible proposal as
the default and mark the affected work item `KEPT_UNRESOLVED`. State the
remaining ambiguity in the public summary; the unresolved marker remains
available for later user correction, but it must not block the next workflow
stage. Every handoff must still make one working decision. When no structural
change should be published, explicitly select `KEEP_CURRENT_STRUCTURE` and
return `NO_CHANGE`; never return a terminal `UNRESOLVED` status. Declare the
evidence independently as `SUFFICIENT`, `CONFLICTING`, or `INSUFFICIENT`.
Evidence quality remains visible but never becomes a per-item workflow blocker.
Never wait for per-item confirmation and never use uncertainty to bypass Host
validation.

After any replacement, ask the Host to materialize each changed scope in source
order, reopen
the rendered output, and run the materialization validator. If validation
fails, consume only the compact failure signals and at most three matching
reminders, revise the failed candidate, and rematerialize within the Host's
stage budget. A repeated failure preserves explicit unresolved evidence while
the Agent selects either the first Host-valid reversible proposal or the current
structure as the working decision; it does not stop unrelated scopes or workflow
advancement. An unverified candidate cannot be marked consumed. Unaffected
scopes continue.

Re-read the proposed ownership and boundary ledgers against the source before
submitting. The audit must establish all of the following:

- every page has exactly one primary document-unit/attachment decision;
- every non-empty physical table has a complete exact region partition, every
  empty physical table has one explicit disposition, and every source-adjacent
  table boundary has exactly one of the five working decisions;
- every multi-region physical detection has an exact cell partition, and no
  sibling logical regions were forced into one whole-table scope;
- every merged scope has a compatible result for all seven comparison
  dimensions and at least one explicit endpoint-continuation source ref;
- no pair marked `SEPARATE` shares a logical-table scope, and no `NESTED`,
  `FALSE`, or `UNRESOLVED` table is smuggled into one;
- attachment/title restarts were not merged merely because topic, column count,
  headers, or schemas look alike;
- no redundant section, duplicate source span, empty leaf, or broad attachment
  container obscures the independently owned objects beneath it;
- every grounded hierarchical numbered heading has exactly one structural
  object, while table/figure captions and page furniture have no standalone
  section object;
- every present table raw-text, notes, and visual residual has an explicit
  semantic placement or remains explicitly unresolved;
- all remaining ambiguity is explicitly represented by `UNRESOLVED`, grounded
  reason codes, and a `PARTIAL` result; a selected working proposal is clearly
  marked as the reversible default rather than asserted as source truth.

Persist the structure artifact only after this self-audit and Host
ownership/coverage validation pass. If a later parent family contradicts page
or table ownership, revise the structure artifact instead of silently repairing
it downstream.

Do not turn physical rows into parent records in D2, join tables by proximity alone,
repair numbering, create requirements, or force the proposal into the legacy
StructureTree.

## Required ownership contract

The V3 runner output exposes unified non-table `logical_objects[]`, exact
`table_regions`, region scope/object placements,
`supplemental_evidence_placements[]`, and unowned non-table
`source_evidence_dispositions[]`. The Host derives stable V2 logical objects,
region source-segment references, table replacement ledgers, and validation
receipts. Cell evidence remains in the immutable table-region artifact and is
referenced by region ID/SHA rather than copied into multiple objects. Source
segments and object-bearing regions are globally single-owner; nested, false,
unresolved, empty-table, and supplemental evidence remain explicit.

The whole-table V2 runner wire and V1 proposal are compatibility-only controls
for historical comparison. Do not emit them in the default workflow, and do
not let their former expressiveness gap force a representable V3 region
partition to `UNRESOLVED`.

Do not emit parent provisions, table-record parents, or requirement
classifications during document structure recovery.
