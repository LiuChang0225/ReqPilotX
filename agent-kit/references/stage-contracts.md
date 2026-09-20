# D0-D6 stage contracts

Terminology override: D3 produces a `SourceItemBaseline`; the existing
`SourceRequirementBaselineArtifactV1` name is wire compatibility. D4 is
Requirement Decomposition and produces `SingularRequirement` children. Public
assembly is one progressive ledger over the accepted source-document hierarchy,
not a parallel record list. The ledger also carries the draft ReqIF 1.2 mapping
identity, but ReqIF export remains a separate Host capability.

D0-D6 are seven lifecycle stages. D0 is Host-owned; D1-D6 use six capability
Skills. Neither number describes document chunks.

| Stage | Input authority | Output |
|---|---|---|
| D0 Source preflight | authorized bytes, role, hash, page count | `DocumentRunManifestV2` |
| D1 Document understanding | D0 + page/native/visual evidence | `DocumentUnderstandingProposalV2` |
| D2 Logical structure | validated D1 + table/asset/parser/resolver evidence | `LogicalStructureProposalV1` |
| D3 Source item baseline | accepted source-document hierarchy + exact text/table evidence | `SourceRequirementBaselineArtifactV1` compatibility artifact + `SourceItemLedger` projection |
| D4 Decomposition | validated D3 parents and shared evidence | `RequirementDecompositionProposalV2` |
| D5 Verification | original evidence + workflow-specific artifacts, never primary reasoning | `ExtractionVerificationReportV1` |
| D6 Result assembly | accepted artifact chain for exactly one workflow | `AgentExtractionReadModelV1` |

There are two product workflows over these capabilities:

1. **Requirement baseline**: D0, D1, D2, D3, Host baseline verification, then
   deterministic public assembly. It preserves accepted Document Map context,
   source-faithful SourceItems, requirement judgment, SRC/REQ and exact table/
   text/PDF evidence; D4 is not invoked. Direct text is compiled from exact
   Unicode spans; logical tables are compiled from Agent-owned semantic row
   groups or product branches. A cold D5 Agent is signal-triggered, not a
   mandatory extra model turn, and D6 does not need a model to serialize an
   already validated baseline.
2. **Explicit atomization**: accepted baseline + user-selected requirement
   scope, then D4, atomization-mode D5, and a derived D6 projection. It never
   changes or replaces the baseline artifact.

Every D5 and D6 artifact declares `workflow_mode` as `BASELINE` or
`ATOMIZATION`. Baseline mode rejects a D4 predecessor; atomization mode rejects
a missing accepted baseline, missing explicit trigger, or a scope that differs
from the user-selected parent IDs.

Requirements analysis is downstream of the accepted baseline/atomization
choice and must not implicitly start D4.

An artifact binds its proposal payload, evidence set, validator receipt, source
identity, Kit identity, and predecessor artifacts. A later stage cannot silently
rewrite an earlier artifact. Corrections create a new version and invalidate
dependent artifacts.

Write and validate each stage artifact when that stage ends. Do not keep D1-D4
only in model context and serialize all artifacts at the end: late batch writes
allow stages to contradict one another without a predecessor gate. Before the
next stage starts, bind the predecessor artifact ID/hash/status and verify that
its declared page, table, object, and any already-declared record ownership
remains unchanged. D2 does not predeclare D3 table-record parents.

Every artifact field that claims source identity must resolve inside the exact
current workspace bundle. Missing or stale source handles, cells, tables,
assets, placements, or pages are grounding failures, even when the quoted
semantic statement happens to be correct.

D4 additionally binds exact character ranges inside every accepted source
requirement record.
Ranges may disposition text as assertion, facet, context, or unresolved, but
must cover every non-whitespace character once with no overlap. This exact-cover
gate applies only after the Agent has selected a source-grounded parent; it does
not restore a parser-defined parent universe or decide semantic atomicity.
