# Judgment, decomposition and response cases

First establish the source role and complete governed statement. The examples are invented; their values are not engineering recommendations. Class, content role, applicability and analysis readiness answer different questions.

## Ordinary classification

| Case / synthetic source | Judgment | Countercase |
|---|---|---|
| J01 `The module shall transmit a fault message on detection` | FUNCTIONAL: required behavior and trigger | Reporting the engineering review result is a PROCESS duty, not product communication behavior |
| J02 `Response time shall not exceed 15 ms under condition C` | QUALITY: measured property with its condition and bound | A required interface implementation or material selection is CONSTRAINT; the presence of numbers/standards does not decide |
| J03 `The connector shall use interface X` | CONSTRAINT: imposed implementation/interface choice | Connector insertion-force limits are QUALITY; a required diagnostic function is FUNCTIONAL |
| J04 `Supply the interface report before sample release`; `record and approve changes before implementation` | First is a PROJECT deliverable/milestone; second a PROCESS obligation | A heading named “quality” does not make its audit/delivery text product QUALITY. Sample counts or test duration inside one method remain test context unless an independent obligation exists |
| J05 A principle explanation, definition, example or descriptive product table | Retained non-requirement when explanatory; use its actual content role | A table value or test equipment/objective can inherit normative force without “shall/应”. Read the governing section and purpose/method/criteria together. Required test configuration stays with its verification target; only a separate execution duty is PROCESS. Neither lack of a modal word nor a heading called “information” proves non-requirement |
| J06 A standards list and an informative annex | Retain references and source-declared normative/informative roles | An explicit “the following documents apply” provision can be a requirement while list entries remain referenced content. A bare citation does not import every external clause or establish compliance. Follow this document's drafting conventions; do not classify arbitrary SOR notes solely by ISO-style wording |

### Capture completeness and content judgment

A parts catalogue that only describes available products can be retained reference content. A BOM whose notes prescribe wiring, materials or assembly carries constraints even if its heading says “product information”; keeping the BOM whole does not make it non-requirement. Document-control metadata means facts such as document revision/date, not all descriptive product text.

Incomplete capture is not a content role. If the original visibly requires a
supplier's acceptance target but capture retains only `RFQ` or `98`, those
fragments do not make the provision METADATA or a non-requirement. Judge the
visible original and recover a source-bound reading. If the available tool
cannot encode the complete unit, record its exact unresolved source range;
do not manufacture a metadata SRC to make compilation appear complete.

### Verification target versus a separate process obligation

`After conditioning by method M, insulation resistance shall remain above R` is a QUALITY target with verification context. The required specimen state, equipment, cycles and measurement method stay with that target; their presence does not itself add PROCESS. VERIFICATION is a topic when applicable, not a replacement or automatic second class.

`The supplier shall maintain calibration records and approve procedure changes` expresses a PROCESS obligation. `Submit the validation report at milestone T` expresses a PROJECT deliverable. If an original provision actually contains both a measured product target and an independent execution/control duty, preserve both meanings; do not apply a blanket “tests can never be PROCESS” rule. Classify the governed obligation, not the occurrence of “test”, an imperative verb or a verification-table heading.

### Definitions and applicability across sections

`Settling time means the interval from event A to stable output B` can remain a DEFINITION while governing a later settling-time limit. Keeping the definition as a separate SRC does not by itself link it to the analyzed requirement. Read the original applicability and preserve the relation through existing context_source_ids; before splitting, check that the parent and its relevant context support the child response.

Countercases: a similarly named metric for another product variant, a definition limited to an unrelated annex, or an informative example need not govern this item. The same word is only a reason to investigate. Shared table headers and governing notes can already provide context without becoming new SRCs. Preserve necessary direct/shared context and explicit links without duplicating the original or linking every definition indiscriminately.

## Decomposition and response

| Case | Handling and countercase |
|---|---|
| J07 Independent obligations versus one complete target | Unnumbered `submit a report and organize a design review` can remain one original parent with two authorized response children. Different independent responses justify the split, not merely different classes. Conversely temperature, voltage, repetitions and tolerances jointly defining one target/test normally stay together. Original numbered peers are source containment, not Snn. Record not-reviewed, keep-whole with reason, or split with source basis; child classes follow their own meaning |
| J08 Alternatives, options, TBD and conflicts | Preserve `A or B`, inclusive/strict bounds, negation, phase, subject and exceptions. Optional content may express a conditional requirement, not a mandatory current commitment; a selected choice and an ordinary enumerated list differ. TBD can remain a requirement with missing criteria. For conflicting values compare units, variant, phase, date and scope first; explain a valid unit conversion in analysis without rewriting source. Unresolved contradictions remain source-linked questions, not averaged values or product non-compliance |
| J09 Internal evidence and engineering responses | No supplied internal evidence: skip automatic compliance analysis. Evidence supplied but insufficient for an attempted item: record the precise gap. Customer criteria undefined/conflicting: clarification. A matching keyword, standard title, old proposal or different product's test does not prove compliance; cite the applicable revision, locator, conditions and result. Conflicting internal evidence follows established authority/version rules, otherwise stays unresolved. For one criterion of ≤20 ms, an eligible same-condition result of 12 ms supports it, 25 ms contradicts it, and a different product’s result is insufficient. Engineer opinions take priority where valid; never label them customer approval or reuse a stale opinion after material context changes |
| J10 Revision and delivery | Fix a wrong source boundary/owner in the source model; fix correct-data/wrong-display in the reader/exporter. Compare the same version and scope through Map → items/relationships → snapshot → UI/Excel before blaming mapping. Keep original parents, current effective responses, source order and stable IDs; collapsed UI rows do not define export scope. Body loss, missing images, clipped display and a missing analysis attempt are different failures; successful export/hash checks do not establish semantic quality |

Scope applies throughout: a footer rule, table note or liability exception governs only its proven targets. Two items sharing a page/topic do not inherit each other's conditions. Read the necessary upper, adjacent and referenced context even when a tool window or UI filter omits it.
