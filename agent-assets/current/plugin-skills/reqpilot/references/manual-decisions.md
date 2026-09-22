# Explicit user working opinions

Technical engineer opinions are separate from Agent assessments and source classification overrides. Import only decisions that the user explicitly supplied. Do not invent, infer or relabel an Agent decision as a human one. These opinions are working notes, not formal customer acceptance.

Inspect the current actual units with:

```bash
python3 scripts/reqpilot.py manual-context --baseline-dir <current-version>
```

The user input file has the following exact envelope; copy current source/baseline SHA and actual unit IDs from the context. An Snn uses its stable singular requirement ID, not a guessed display code.

```json
{
  "schema_version": "source-item-user-decisions/v1",
  "expected_source_sha256": "<current source SHA256>",
  "expected_baseline_sha256": "<current baseline SHA256>",
  "user_input_reference": "<reference to the actual explicit user input>",
  "decisions": [
    {
      "unit_id": "<actual unit ID>",
      "action": "SET",
      "decision": "PENDING_EVIDENCE",
      "reason": "<user-provided reason>",
      "conditions": "<user-provided conditions, or empty>",
      "evidence_requests": "<user-provided request, or empty>"
    }
  ]
}
```

Decisions are `COMPLIANT`, `PARTIALLY_COMPLIANT`, `NON_COMPLIANT`, `PENDING_EVIDENCE`, `NEEDS_CLARIFICATION`, or `NOT_APPLICABLE`. Withdrawal uses `action: WITHDRAW` and an actual user reason. The program retains previous opinions in history.

```bash
python3 scripts/reqpilot.py import-manual-decisions \
  --baseline-dir <current-version> --input <user-decisions.json> \
  --output-dir <new-user-import>
python3 scripts/reqpilot.py review-revise \
  --review-dir <review> --input <revision-input.json> \
  --manual-decisions <new-user-import/manual-decisions.json> \
  --output-dir <review/versions/user-opinion>
```

The ordinary revision input still binds the current review/baseline SHA. A user-opinion replacement is its own revision; do not mix it with source, classification, Map or analysis replacements. Keep the generated import receipt, exact input copy and previous-manual artifact alongside the new artifact. `review-close` and `review-verify` replay the import against its true predecessor. A following source revision automatically carries unaffected opinions and marks changed units stale; stale opinions never automatically revive.

`deliver` consumes `manual-decisions.json` from the current version automatically. For a standalone imported artifact, pass `--manual-decisions` with its replayable import directory. Export preserves AI and source state while giving CURRENT user opinions effective priority. It uses the internal program, without a second model analysis of Excel. The import receipt proves reproducibility of supplied bytes, not a person's identity or formal approval.

Native App Server Agent tools cannot invoke the import or provide manual overrides. Application publication uses its current user repository under the document/source transaction lock. Preserve existing human records; the model must never create, withdraw or replace them through an inline review object or arbitrary file argument.
