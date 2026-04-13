# Extraction Batch Summary

- batch_id: `legacy_benchmark_core`
- run_id: `extraction_quality_loop_20260409_05`
- description: `Core extraction regression pack with Q-JLY, BAIC SOR, damper PDS, and oil pump PDS.`
- shared_repo_root: `/Users/liuchang/Documents/New project`

## Gates
- batch-report: `PASS` | docs=4 issues=30
- regression-suite: `FAIL` | quality_pass=False fidelity_pass=False section_split_pass=True quality_exit_code=1 fidelity_exit_code=1 missing_required_profiles=[]
- oil-pump-benchmark: `FAIL` | quality_pass=False fidelity_pass=False pdf=产品设计规格说明书PDS-DE前后油泵总成-V1.1-20251215.pdf

## Issue Rollup
- total_issues: `30`
- category_counts: `{'benchmark_coverage': 5, 'content_fidelity': 7, 'outline_structure': 18}`
- severity_counts: `{'high': 12, 'medium': 18}`

## Section Split
- summary: `{'targets': 1, 'targets_passed': 1, 'checks_passed': 4, 'checks_total': 4, 'score_pct': 100.0, 'results': [{'doc_key': 'q_jly_core', 'pdf_name': 'Q-JLYJ7111029E-2024-汽车电气和电子零部件通用技术规范.pdf', 'passed': True, 'score_pct': 100.0}]}`

## Documents
- `q_jly_core` -> `Q-JLYJ7111029E-2024-汽车电气和电子零部件通用技术规范.pdf` | quality `90.0%` | fidelity `81.82%` | section_split `True`
- `sor_baic_core` -> `SOR-N82KS-E01项目-T00007918-氮氧传感器-签批.pdf` | quality `90.0%` | fidelity `86.36%` | section_split `True`
- `pds_de_active_damper_v1_0` -> `产品设计规格说明书PDS-DE全主动减振器总成-V1.0-20251127.pdf` | quality `100.0%` | fidelity `81.82%` | section_split `True`
- `pds_de_oil_pump_v1_1` -> `产品设计规格说明书PDS-DE前后油泵总成-V1.1-20251215.pdf` | quality `90.0%` | fidelity `95.45%` | section_split `True`
