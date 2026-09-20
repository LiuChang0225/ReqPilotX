---
name: reqpilotx-result-assembly
description: Assemble accepted ReqPilotX extraction stage artifacts into a runner-neutral, evidence-linked read model without reinterpreting source facts. Use for D6 after verification.
---

# Result assembly

Produce `AgentExtractionReadModelV1` from the accepted artifacts of exactly one
workflow. Baseline mode consumes D0, D1, D2, D3, and baseline-mode D5;
requirement-decomposition mode consumes an accepted baseline plus D4 and decomposition-mode D5.
Read the [core ontology](../../../references/core-ontology.md), [stage
contracts](../../../references/stage-contracts.md), and [runner
parity](../../../references/runner-parity.md).

If D5 rejects any predecessor, produce only a failure/readiness projection with
the responsible stage and allowed recovery actions. Do not assemble rejected
content into an active extraction read model.

## Method

1. Verify and expose `workflow_mode`; reject a mixed, missing, or out-of-order
   artifact chain.
2. Preserve artifact versions, source identity, predecessor links, verification
   findings, alternatives, and unresolved scopes.
3. Project containers, logical records, parent provisions, assertions, facets,
   context, references, and evidence handles without changing their semantics.
   Baseline mode must not manufacture assertions. Atomization mode keeps every
   baseline parent visible and marks all accepted children as a derived layer.
4. Expose page/table/region navigation, status, risk, and disagreement fields
   needed by the ReqPilotX Workbench.
5. Keep historical or legacy comparisons explicitly separate from the active
   read model.
6. Validate that every displayed item maps to an accepted artifact and that no
   hidden WorkingTree, provider, node, or chunk state defines completion.

Do not reclassify, merge, split, repair, Apply, publish, or infer missing
content during assembly. A D6 artifact is still a non-serving proposal until a
separate product activation decision permits otherwise.
