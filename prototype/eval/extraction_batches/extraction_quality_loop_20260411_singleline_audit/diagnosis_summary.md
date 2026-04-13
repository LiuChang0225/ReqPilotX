# Extraction Diagnosis Summary

- docs: `4`
- findings: `73`
- pattern_candidates: `12`
- docs_requiring_human_review: `2`
- suspected_layer_counts: `{'extractor': 11, 'gate': 5, 'parser': 2, 'preview': 55}`

## Documents
- `q_jly_core` -> `Q-JLYJ7111029E-2024-汽车电气和电子零部件通用技术规范.pdf` | profiles `{'quality': 'q_jly_2024', 'fidelity': 'q_jly_core'}` | findings `54` | pattern_candidates `4`
  - `local_clause_section_missing` [preview] 5.3.6.3 should expose local section anchors such as 2, 3, 4
  - `local_clause_section_missing` [preview] 5.1.14.2 should expose local section anchors such as 2, 3, 4
  - `local_clause_section_missing` [preview] 4.4.3 should expose local section anchors such as 3
- `sor_baic_core` -> `SOR-N82KS-E01项目-T00007918-氮氧传感器-签批.pdf` | profiles `{'quality': 'sor_baic_core', 'fidelity': 'sor_baic_core'}` | findings `10` | pattern_candidates `4`
  - `local_clause_section_missing` [preview] 15.2.2.4 should expose local section anchors such as 0
  - `local_clause_section_missing` [preview] 15.2.4.1 should expose local section anchors such as 0
  - `complex_table_clause_binding_drift` [preview] Complex-table pages should keep stable section ownership while preserving row coverage.
- `pds_de_active_damper_v1_0` -> `产品设计规格说明书PDS-DE全主动减振器总成-V1.0-20251127.pdf` | profiles `{'quality': 'pds_de_active_damper_v1_0', 'fidelity': 'pds_de_active_damper_v1_0'}` | findings `4` | pattern_candidates `2`
  - `complex_table_clause_binding_drift` [preview] Complex-table pages should keep stable section ownership while preserving row coverage.
  - `required_title_mismatch` [extractor] Missing or drifted section anchors should be restored through generic heading recovery.
  - `required_title_mismatch` [extractor] Missing or drifted section anchors should be restored through generic heading recovery.
- `pds_de_oil_pump_v1_1` -> `产品设计规格说明书PDS-DE前后油泵总成-V1.1-20251215.pdf` | profiles `{'quality': 'pds_de_oil_pump_v1_1', 'fidelity': 'pds_de_oil_pump_v1_1'}` | findings `5` | pattern_candidates `2`
  - `local_clause_section_missing` [preview] 5.6.2.1.2 should expose local section anchors such as 5
  - `local_clause_section_missing` [preview] 5.2.4 should expose local section anchors such as 5
  - `local_clause_section_missing` [preview] 5.3 should expose local section anchors such as 5
