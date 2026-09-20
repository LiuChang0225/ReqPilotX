---
name: reqpilotx-requirement-decomposition
description: Decompose an explicitly user-selected scope of accepted RequirementItems into derived, independently verifiable SingularRequirements while preserving every source item. Use only for a separate D4 requirement-decomposition run; never run automatically during baseline extraction.
---

# Requirement decomposition

Produce `RequirementDecompositionProposalV2` only for the user-selected,
eligible requirements in an accepted baseline. Read [requirements-engineering
baseline](../../../references/requirements-engineering-baseline.md), then load
the [core ontology](../../../references/core-ontology.md), [evidence
authority](../../../references/evidence-authority.md), [stage
contracts](../../../references/stage-contracts.md), and [failure
policy](../../../references/failure-policy.md).

## Method

1. Verify `explicit_trigger_required=true`, accepted baseline ID/SHA, user
   scope, and exact selected parent IDs before model execution. Never infer an
   all-document scope from baseline completion.
2. Preserve the complete parent and all source lineage.
3. Create an assertion only when it is independently verifiable. Preserve its
   subject, action, object, condition, limit, unit, negation, exception,
   applicability, and referenced qualifier.
4. Represent method, stage, sample, cycle, parameter, evidence, reference, and
   note as facets or context unless the source independently makes them an
   obligation.
5. Copy source identities only from the exact current workspace evidence; never
   construct a handle from memory, a prior run, or a nearby node. Emit a typed
   reference for every assertion and facet that makes a source claim.
6. Run `scripts/validate_bundle_references.py` against the draft artifact and
   exact workspace pins. Any dangling handle, cell, table, asset, placement, or
   page makes D4 invalid; retain the semantic content as unresolved until it is
   rebound to existing evidence.
7. For each decomposition, emit `source_coverage[]`: one entry for every D3
   `source_handle`, with non-overlapping character ranges whose disposition is
   `ASSERTION`, `FACET`, `CONTEXT`, or `UNRESOLVED` and whose `target_id` names
   the projected item. Run `scripts/validate_parent_source_coverage.py`; every
   non-whitespace source character must be dispositioned exactly once.
8. When two clauses can fail independently, create separate assertions even if
   they share a sentence, list item, or parent. Exact source coverage prevents
   omission; it does not by itself prove atomicity.
9. Label a child `VERBATIM_SPAN` only when it is exact source text; otherwise
   label it `NORMALIZED_DERIVATION`. Derived wording never replaces the visible
   baseline parent or its provenance.
10. Disclose every non-assertion or unresolved source scope. Reclassify a
   zero-obligation parent rather than inventing an assertion.
11. Persist the validated D4 artifact before D5; do not defer all stage writes to
   end-of-run assembly.

Do not split mechanically at sentences, paragraphs, punctuation, modal words,
list markers, table rows, or cells. Do not summarize away source meaning or map
the output back into a required legacy node owner.
