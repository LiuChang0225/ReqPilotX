# Extraction Diagnosis Summary

- docs: `1`
- findings: `2`
- pattern_candidates: `2`
- docs_requiring_human_review: `1`
- suspected_layer_counts: `{'parser': 1, 'preview': 1}`

## Documents
- `seres_q_sk_j02_311` -> `Q-SKJ02.311-2025_UDSonIPCANCANFD诊断技术规范.pdf` | profiles `{'quality': 'seres_q_sk_j02_311', 'fidelity': 'seres_q_sk_j02_311'}` | findings `2` | pattern_candidates `2`
  - `complex_table_clause_binding_drift` [preview] Complex-table pages should keep stable section ownership while preserving row coverage.
  - `parser_low_confidence_hotspot` [parser] Low-confidence pages should be stabilized before downstream clause binding is tuned.
