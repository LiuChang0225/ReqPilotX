# Extraction Batch Summary

- batch_id: `legacy_benchmark_core`
- run_id: `legacy_benchmark_core_20260418_j02311_fix13`
- description: `Core extraction regression pack with Q-JLY, BAIC SOR, damper PDS, and oil pump PDS.`
- shared_repo_root: `/Users/liuchang/Documents/New project`

## Gates
- batch-report: `PASS` | docs=4 issues=83
- regression-suite: `FAIL` | quality_pass=False fidelity_pass=False section_split_pass=True quality_exit_code=1 fidelity_exit_code=1 missing_required_profiles=[]
- oil-pump-benchmark: `FAIL` | quality_pass=True fidelity_pass=False pdf=产品设计规格说明书PDS-DE前后油泵总成-V1.1-20251215.pdf

## Issue Rollup
- total_issues: `83`
- category_counts: `{'benchmark_coverage': 50, 'content_fidelity': 29, 'outline_structure': 4}`
- severity_counts: `{'high': 77, 'medium': 6}`

## Diagnosis
- findings: `73`
- pattern_candidates: `10`
- suspected_layer_counts: `{'extractor': 15, 'parser': 2, 'preview': 56}`
- diagnosis_summary: `/Users/liuchang/Documents/New project/worktrees/_active/extraction-quality-loop/prototype/eval/extraction_batches/legacy_benchmark_core_20260418_j02311_fix13/diagnosis_summary.md`

## Section Split
- summary: `{'targets': 1, 'targets_passed': 1, 'checks_passed': 4, 'checks_total': 4, 'score_pct': 100.0, 'results': [{'doc_key': 'q_jly_core', 'pdf_name': 'Q-JLYJ7111029E-2024-汽车电气和电子零部件通用技术规范.pdf', 'passed': True, 'score_pct': 100.0}]}`

## Documents
- `q_jly_core` -> `Q-JLYJ7111029E-2024-汽车电气和电子零部件通用技术规范.pdf` | quality `100.0%` | fidelity `91.67%` | section_split `True` | diagnosis `47` findings
- `sor_baic_core` -> `SOR-N82KS-E01项目-T00007918-氮氧传感器-签批.pdf` | quality `90.0%` | fidelity `75.0%` | section_split `True` | diagnosis `14` findings
- `pds_de_active_damper_v1_0` -> `产品设计规格说明书PDS-DE全主动减振器总成-V1.0-20251127.pdf` | quality `90.0%` | fidelity `70.83%` | section_split `True` | diagnosis `7` findings
- `pds_de_oil_pump_v1_1` -> `产品设计规格说明书PDS-DE前后油泵总成-V1.1-20251215.pdf` | quality `100.0%` | fidelity `95.83%` | section_split `True` | diagnosis `5` findings
