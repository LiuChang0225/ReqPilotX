# Fidelity Benchmark

- PDF dir: `/Users/liuchang/Documents/New project/worktrees/_active/extraction-quality-loop/prototype/eval/extraction_batches/extraction_quality_loop_20260409_05/staged_docs`
- Target docs: `4`
- Checks: `76/88`
- Score: `86.36%`
- Failed docs: `4`
- Overall pass: `False`

## Document Scores
- `Q-JLYJ7111029E-2024-汽车电气和电子零部件通用技术规范.pdf` (q_jly_core): `81.82%`, checks `18/22`
- `产品设计规格说明书PDS-DE全主动减振器总成-V1.0-20251127.pdf` (pds_de_active_damper_v1_0): `81.82%`, checks `18/22`
- `SOR-N82KS-E01项目-T00007918-氮氧传感器-签批.pdf` (sor_baic_core): `86.36%`, checks `19/22`
- `产品设计规格说明书PDS-DE前后油泵总成-V1.1-20251215.pdf` (pds_de_oil_pump_v1_1): `95.45%`, checks `21/22`

## Top Issues
- `Q-JLYJ7111029E-2024-汽车电气和电子零部件通用技术规范.pdf` | `section_count_drift` | `` | sections=433 expected=399 drift=34
- `Q-JLYJ7111029E-2024-汽车电气和电子零部件通用技术规范.pdf` | `requirement_count_drift` | `` | requirements=532 expected=398 drift=134
- `Q-JLYJ7111029E-2024-汽车电气和电子零部件通用技术规范.pdf` | `section_consistency_issue_limit` | `` | consistency_issues=8 max=3
- `Q-JLYJ7111029E-2024-汽车电气和电子零部件通用技术规范.pdf` | `forbidden_code_present` | `4.1.2.2.2` | forbidden scoped code present
- `SOR-N82KS-E01项目-T00007918-氮氧传感器-签批.pdf` | `section_consistency_issue_limit` | `` | consistency_issues=2 max=1
- `SOR-N82KS-E01项目-T00007918-氮氧传感器-签批.pdf` | `required_code_missing` | `F:1` | required scoped code missing
- `SOR-N82KS-E01项目-T00007918-氮氧传感器-签批.pdf` | `required_code_missing` | `G:1` | required scoped code missing
- `SOR-N82KS-E01项目-T00007918-氮氧传感器-签批.pdf` | `required_title_mismatch` | `E:2` | title '通讯协议：按照 EMS 系统通讯协议开发' does not contain '系统职责'
- `产品设计规格说明书PDS-DE全主动减振器总成-V1.0-20251127.pdf` | `requirement_count_drift` | `` | requirements=269 expected=219 drift=50
- `产品设计规格说明书PDS-DE全主动减振器总成-V1.0-20251127.pdf` | `required_requirement_text_mismatch` | `3.2.2` | requirement text does not contain '7.3.1 ）外观磕伤、划伤标准'
- `产品设计规格说明书PDS-DE全主动减振器总成-V1.0-20251127.pdf` | `required_table_row_snippet_missing` | `3.2.2` | table row snippet not found: '16 法规'
- `产品设计规格说明书PDS-DE全主动减振器总成-V1.0-20251127.pdf` | `forbidden_cross_section_source_missing` | `3.2.2` | source does not contain snippet: '7.3.1 ）外观磕伤、划伤标准'
- `产品设计规格说明书PDS-DE前后油泵总成-V1.1-20251215.pdf` | `requirement_count_drift` | `` | requirements=190 expected=99 drift=91

## Missing Profiles
- `esigned_emc_88033918`
- `esigned_et_88033919`
- `qc_t_1067_monitor`

## Skipped Docs
- None
