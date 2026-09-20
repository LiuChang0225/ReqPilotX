# Explicit decomposition

Command details for this topic; [tool index](tools.md) provides the common entry.

Select exact SourceItem IDs from the current baseline, then query their original bindings:

```bash
python3 <plugin>/scripts/reqpilot.py decomposition-packet --baseline-dir <baseline-dir> --source-item-id <source-record-id> --output-dir runtime-data/run/parent-packets
```

Each packet contains numbered source segments, their text/hash/locator, the full original parent identity, shared table context and the baseline SHA. The packet is a convenient source slice; still read the full applicable Map path, upper text, references and original pages.

Write this transient decision envelope, using actual values from the packet and the user's request:

```json
{
  "schema_version": "reqpilot-decomposition-decisions/v1",
  "baseline_sha256": "<current baseline artifact SHA>",
  "authorization": {
    "request": "<the actual explicit user instruction authorizing this scope>",
    "source_item_ids": ["<source-record-id>"]
  },
  "decisions": [
    {
      "source_item_id": "<source-record-id>",
      "action": "SPLIT",
      "logic": "ALL_OF",
      "reason": "<materially different response or acceptance outcomes>",
      "children": [
        {
          "text": "<source-faithful child expression>",
          "derivation_type": "VERBATIM_SPAN",
          "source_ranges": [{"segment_index": 0, "start_offset": 0, "end_offset": 40}],
          "requirement_kinds": ["QUALITY"]
        },
        {
          "text": "<second child expression>",
          "derivation_type": "NORMALIZED_DERIVATION",
          "source_ranges": [{"segment_index": 0, "start_offset": 41, "end_offset": 90}],
          "requirement_kinds": ["QUALITY"]
        }
      ],
      "context_ranges": []
    }
  ]
}
```

Offsets above illustrate the shape only: compute actual half-open character offsets against the exact packet. Every non-whitespace parent character must belong exactly once to a child's range or a `context_ranges` entry such as `{"source_range":{"segment_index":0,"start_offset":90,"end_offset":110},"disposition":"CONTEXT"}`. The existing exact-cover validator also accepts explicit UNRESOLVED context; this is a disclosed gap, not source completion. Parent context is retained without claiming that every condition applies to every child.

A split needs at least two useful children; use action KEEP to continue with the complete original, or UNDO to retire an applied working split. KEEP/UNDO need a source ID and material reason and do not require invented children. Declare ALL_OF versus ANY_OF according to original logic; alternatives do not become simultaneous obligations. Normalized expressions cannot introduce unsupported numeric values, and every normalized child still needs source-driven semantic review. The compiler issues Snn and sealed artifact hashes; do not type these into decisions or manually seal an artifact. Whole IMAGE SourceItems are directly analyzable and do not need text-range decomposition.

```bash
python3 <plugin>/scripts/reqpilot.py compile-decomposition --baseline-dir <baseline-dir> --input <decomposition-decisions.json> --output-dir runtime-data/run/decomposition-v1
```

For revision/undo add `--previous-decomposition <prior-source-item-decomposition.json>`. The immutable original baseline stays unchanged. A sealed hash proves integrity against accidental changes, not cryptographic authorship, semantic correctness or engineer acceptance.

## Saved decisions and remaining scope

The compiler now retains `dispositions[source_item_id]` with the actual KEEP/UNDO/SPLIT reason and source evidence. `review_scope` records the authorized, decided and remaining source identities; the CLI returns both `DECOMPOSITION_COMPILED` (the write succeeded) and `scope_status`/`review_scope` (whether the authorized review is complete). A decision for one of thirteen authorized parents may be saved but remains PARTIAL. Use the returned remaining identities to continue; do not narrow the authorization envelope merely to obtain COMPLETE. `--previous-decomposition` carries earlier reasons and scope forward on the same baseline.

Legacy artifacts stay readable without invented KEEP reasons. A source/classification/context change rechecks retained decisions and makes affected scope pending again. This status is about the explicitly authorized scope, not proof that the entire document was analyzed.
