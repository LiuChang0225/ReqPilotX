# Extraction Diagnosis Summary

- docs: `1`
- findings: `8`
- pattern_candidates: `4`
- docs_requiring_human_review: `1`
- suspected_layer_counts: `{'extractor': 2, 'gate': 2, 'parser': 1, 'preview': 3}`

## Documents
- `seres_q_sk_j02_311` -> `Q-SK J02.311-2025_UDS on IP、CAN、CANFD诊断技术规范.pdf` | profiles `{'quality': 'seres_q_sk_j02_311', 'fidelity': 'seres_q_sk_j02_311'}` | findings `8` | pattern_candidates `4`
  - `local_clause_section_missing` [preview] 8.10.1.2 should expose local section anchors such as 8
  - `local_clause_section_missing` [preview] 7.6.3.2 should expose local section anchors such as 7
  - `complex_table_clause_binding_drift` [preview] Complex-table pages should keep stable section ownership while preserving row coverage.
