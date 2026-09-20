# Core extraction ontology

Load this reference for D2-D6 decisions.

- A **container** organizes source material but is not itself a requirement:
  document, section, attachment, physical table, logical table, figure.
- A **logical record** is one source-bound record within a repeated or tabular
  structure. Physical rows are evidence for records, not an automatic record
  boundary.
- A **parent provision** is the smallest complete source unit that can be
  evaluated without borrowing an unstated sibling. It may own nested provisions,
  assertions, facets, and context.
- A **nested provision** depends on a grounded parent stem or condition. A list
  marker alone does not prove either nesting or independence.
- A **requirement assertion** is independently verifiable while preserving its
  subject, action, object, condition, limits, units, negation, exceptions, and
  applicability.
- A **facet** describes method, stage, sample, cycle, parameter, evidence,
  reference, or note that qualifies an assertion but is not independently an
  obligation without source evidence.
- **Context** remains visible and source-bound but is not classified as an
  obligation.
- **Unresolved** is a first-class outcome when evidence or authority is
  insufficient. It is not a request to guess.

Containers, records, provisions, assertions, facets, and contexts may have
different boundaries. Do not force them into one parser-node hierarchy.
