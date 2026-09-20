---
name: reqpilotx-extraction-verification
description: Independently verify either a ReqPilotX source-faithful SourceItem baseline (D1-D3) or an explicitly triggered requirement-decomposition proposal (D4) against original evidence. Use only in a cold D5 verifier context and never assume D4 exists in baseline mode.
---

# Extraction verification

Produce `ExtractionVerificationReportV1`. You are a cold verifier: do not read
primary reasoning, old product output, legacy node counts, or a desired answer.
Read the [runner parity](../../../references/runner-parity.md), [core
ontology](../../../references/core-ontology.md), [boundary
examples](../../../references/boundary-examples.md), [evidence
authority](../../../references/evidence-authority.md), and [failure
policy](../../../references/failure-policy.md). Read [logical object
ownership](../../../references/logical-object-ownership.md) before checking
page/table assignments, and read the [requirements-engineering
baseline](../../../references/requirements-engineering-baseline.md) before
checking requirementness or atomicity.

## Method

1. Verify exact source, Kit, policy, tool, model, effort, schema, predecessor
   identity, and `verification_mode`. `BASELINE` accepts D1-D3 and forbids a D4
   predecessor. `ATOMIZATION` requires an accepted baseline, exact selected
   parent scope, explicit user trigger, and D4.
2. Re-open the evidence needed to test page coverage, table continuations,
   logical records, parent merge/split decisions, labels, dispositions,
   assertion fidelity, facets, and terminal source coverage.
3. Rebuild the page-to-unit and physical-table ownership map from source. Fail
   when D2 assigns a table to the wrong logical role, when two stages assign the
   same scope incompatibly, or when D3 introduces an object absent from D2
   without a predecessor revision.
4. Run `scripts/validate_bundle_references.py` on source-claiming artifacts.
   Every handle, cell, table, asset, placement, and page must resolve inside the
   exact current bundle; zero dangling references is required.
5. Run `scripts/validate_stage_lineage.py` for D2/D3 page, logical-object, and
   physical-table ownership. A missing ownership index or cross-stage scope
   conflict is a verification failure, not an optional formatting finding.
6. In `BASELINE` mode, verify complete source disposition, exact parent text,
   classification evidence, requirementness/type separation, and unresolved
   disclosure. Do not require atomic children or run a D3/D4 coverage gate.
7. In `ATOMIZATION` mode, run
   `scripts/validate_parent_source_coverage.py` for D3/D4. It must report exact
   non-whitespace coverage for every selected parent. Then independently test
   atomicity: a range-covered assertion still fails when it merges duties or
   limits that can be verified separately.
8. Align disagreements by stable source/container/record handles, not whole
   JSON hashes or counts.
9. When a `structure-materialization-observation/v1` is supplied, run
   `scripts/validate_structure_materialization.py`. Any finding is a verification
   failure for the responsible materialization stage. Resolve at most three
   matching reminders with the Host dynamic tool when available, otherwise
   `scripts/select_structure_failure_patterns.py`; do not load the complete
   catalog into context or call both routes for the same signals.
10. Verify tool authority and revision lineage. Native/source evidence must be
   unchanged. An advisory tool candidate may be replaced only by an
   Agent-authored candidate that binds the tool-result SHA and supporting
   evidence. After a failed materialization, the next attempt must bind the
   previous receipt, change the candidate, rematerialize, reopen the rendered
   scope, and rerun the validator. Tool use is optional when evidence is already
   sufficient; a missing unnecessary call is not a failure.
11. Report each confirmed error, alternative, or unresolved scope with evidence
   and responsible stage.
12. Submit the report to the stage validator. Never silently edit or replace the
   primary proposal.

Agreement is not proof. A stable output, exact-cover, or zero unresolved does
not pass verification when ontology, evidence, granularity, or fidelity fails.
