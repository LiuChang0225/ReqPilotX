# Make an original item independently understandable

A context link says that this source definition, condition or note governs the
item. Preserve its original source as context, without copying it into a new
obligation. Keeping a definition in the ledger does not make that relationship
available to downstream analysis.

For applicable context, establish **what it defines, where it applies, and what
limits it** from the original. Section ancestry, nearby placement and a matching
word are discovery clues; they do not extend an explicit scope. A definition can
govern a later section, while a chapter condition can exclude one of that
chapter's own children. Local alternatives follow the original's stated scope.

| Original evidence | Relationship to preserve | Relationship to avoid |
| --- | --- | --- |
| “In this document, release delay means the interval between command removal and full disengagement.” Later provisions specify release delay in two modes. | Link that definition to both delay provisions. | Do not link it to an unrelated mass limit. |
| “The following humidity applies to 7.2–7.4.” Section 7.5 has its own dry-storage condition. | Link the shared condition to 7.2–7.4, preserving their separate original items. | Do not extend humidity to 7.5 because it is under section 7. |
| One table note covers a merged parameter group; another is limited to variant B. | Preserve the group note on applicable units and the variant note on B. | Do not attach every note to every row. |

Use current `context_review` / `context_candidates` to locate declared context,
and consult the original for definitions the candidates do not expose. Compare
the **existing set** with the source-supported set: keep valid links, add missing
ones, remove unsupported ones. `context_source_ids` replaces the set. Read back
`linked_context` after a change; a successful write verifies the saved relation,
while the original justifies applicability. When the existing set is correct,
leave it unchanged and complete the requested review.
