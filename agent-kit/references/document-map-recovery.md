# Agent-native Document Map recovery

Use this reference for document understanding and original-structure recovery.
The product artifacts remain separate, but one primary Agent owns one continuous
mission: understand the complete authorized source and grow one evidence-bound
Document Map. Stage names are persistence boundaries, not limits on perception
or tool use.

## Capability parity target

The reference behavior is an evidence-driven exploration loop:

1. inspect the complete page atlas/contact sheet and native indexes to form a
   provisional whole-document model;
2. identify source-declared main documents, attachments, section families,
   table packages, numbering restarts, rotations, and visually distinct ranges;
3. inspect every page in the current semantic scope at readable resolution,
   using native text as wording authority and pixels as layout/visibility
   authority;
4. inspect tables, assets, adjacent pages, and focused regions when they can
   confirm or disprove the current structural hypothesis;
5. revise earlier scope or ownership hypotheses when later evidence conflicts;
6. publish a Host-valid checkpoint after each semantic scope instead of waiting
   for one whole-document JSON object;
7. coordinate only scopes with a real adjacency, continuation, shared-value, or
   parentage dependency; unrelated scopes continue;
8. validate exact page/source/table/asset coverage before accepting the final
   Document Map.

The Agent chooses the order and tools by information gain. The Host may supply a
numbered-heading skeleton, parser grouping, resolver suggestion, visual-risk
flag, or table candidate, but each is advisory navigation evidence. None may
define the Agent's scope count, hierarchy, table role, or completion.

## Visual evidence policy

For reference-parity and new-format qualification runs, open the contact sheet
and every page's high-resolution render. The contact sheet is navigation only;
it does not prove page inspection. Record actual image-view receipts separately
from files that merely exist.

After parity is demonstrated across the declared corpus, a product run may use
risk-adaptive high-resolution review only when its policy identity is explicit
and the evaluator proves no loss of structural fidelity. Even then every page
must remain represented in the page ledger and atlas, and the Agent may open any
authorized page or region when later evidence creates doubt. A Host risk flag is
a priority hint, never a visual-access whitelist.

## Private run state, not extra product stages

The whole-document survey and semantic-scope plan are private checkpoints inside
the original-structure workflow. They do not create another user-visible gate or
a second source hierarchy. The page partition is only for primary document-unit
ownership such as front matter, one main document, or a separately authoritative
attachment. It must not be reused as a chapter boundary: two adjacent chapters
may begin and end on the same physical page. Detailed chapter/section scopes use
exact source spans and may share boundary pages without sharing source facts.

A survey checkpoint must:

- bind the exact source, Kit, page count, and actual visual-open receipts;
- assign every page exactly once to a provisional primary document-unit range,
  including an explicit unresolved range when necessary;
- preserve source order and evidence refs;
- remain revisable as detailed scope work discovers better boundaries.

Each detailed scope checkpoint contains the complete replacement for that scope,
not a prose status or partial patch. The Host validates source identity, exact
ranges, cells, geometry, stable IDs, and revision lineage, then publishes the
verified partial tree to the same UI. An interrupted run resumes from accepted
scope checkpoints without regenerating them.

## Authority boundary

The Agent owns:

- the whole-document mental model and semantic scope plan;
- heading/container hierarchy and direct-content ownership;
- logical-table, continuation, nested-value, false-table, figure, and attachment
  interpretation;
- the decision to inspect more native, visual, table, or adjacent evidence;
- source-grounded revisions and human-readable findings/proposals.

The Host owns:

- immutable source bytes, authorization, hashes, page/cell/bbox facts;
- the read-only Evidence Workspace and safe tool execution;
- exact-cover, overlap, lineage, persistence, scheduling, retry, and publication;
- content-free public Activity derived from real tool and checkpoint receipts.

The Host must not select headings, business table boundaries, or document-unit
semantics on behalf of the Agent. A validator rejection is bounded feedback
about a candidate, not authority to substitute a parser answer.

## Public progress

Expose only observable activity: goal, current semantic range, pages visually
opened, evidence/tool intent, accepted checkpoint, finding, proposal, recovery,
and elapsed time. Do not expose hidden reasoning. Never synthesize progress from
page numbers or replay an accepted final tree as if the Agent were discovering
it.
