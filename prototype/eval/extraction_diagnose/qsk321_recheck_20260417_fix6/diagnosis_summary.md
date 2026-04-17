# Extraction Diagnosis Summary

- docs: `1`
- findings: `2`
- pattern_candidates: `2`
- docs_requiring_human_review: `1`
- suspected_layer_counts: `{'gate': 1, 'parser': 1}`

## Documents
- `seres_q_sk_j02_321` -> `Q-SK J02.321-2025_CAN、CANFD、Ethernet、LIN BootLoader刷写规范.pdf` | profiles `{'quality': '', 'fidelity': ''}` | findings `2` | pattern_candidates `2`
  - `profile_unmatched` [gate] Capture diagnosis evidence first, then decide whether the sample should join a reusable benchmark profile.
  - `parser_low_confidence_hotspot` [parser] Low-confidence pages should be stabilized before downstream clause binding is tuned.
