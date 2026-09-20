# Failure and stopping policy

Load this reference whenever a stage cannot be validated.

- `PARTIAL`: grounded useful output exists and every missing or conflicting
  source scope is explicitly identified.
- `BLOCKED`: required evidence, source authority, declared tool, capability, or
  budget is unavailable before a safe proposal can be produced.
- `FAILED`: runtime or contract execution failed and no validator-safe artifact
  exists.
- `VALIDATED`: the declared stage validator accepted the complete artifact;
  validator-safe does not by itself prove corpus quality.

Do not conceal a failure through lower-resolution rendering, text truncation,
silent provider substitution, legacy fallback, or fewer parents. A corrected
validator attempt may repair serialization or grounded schema errors. Keep
evidence quality independent from the working decision: conflicting or
insufficient evidence lowers confidence and remains visible, while the Agent
still selects the best source-supported reversible proposal. Only genuinely
missing or unreadable source evidence enters object-level recovery; it must not
be mislabeled as semantic uncertainty or stop independent objects.
