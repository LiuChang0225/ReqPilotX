# Extraction Diagnosis Summary

- docs: `1`
- findings: `2`
- pattern_candidates: `2`
- docs_requiring_human_review: `1`
- suspected_layer_counts: `{'parser': 1, 'preview': 1}`

## Documents
- `seres_v32_k89_cts_asc` -> `【机密】V32&K89_CTS_ASC_V1.1.0_20250630 (1).pdf` | profiles `{'quality': 'seres_v32_k89_cts_asc', 'fidelity': 'seres_v32_k89_cts_asc'}` | findings `2` | pattern_candidates `2`
  - `complex_table_clause_binding_drift` [preview] Complex-table pages should keep stable section ownership while preserving row coverage.
  - `parser_low_confidence_hotspot` [parser] Low-confidence pages should be stabilized before downstream clause binding is tuned.
