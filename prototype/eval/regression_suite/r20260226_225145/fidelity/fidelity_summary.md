# Fidelity Benchmark

- PDF dir: `/Users/liuchang/Downloads/Test pdf`
- Target docs: `7`
- Checks: `80/91`
- Score: `87.91%`
- Failed docs: `3`
- Overall pass: `False`

## Document Scores
- `产品设计规格说明书（PDS）-DE前后油泵总成-V1.1-20251215.pdf` (pds_de_oil_pump_v1_1): `61.54%`, checks `8/13`
- `产品设计规格说明书（PDS）-DE全主动减振器总成-V1.0-20251127.pdf` (pds_de_active_damper_v1_0): `69.23%`, checks `9/13`
- `SOR-N82KS-E01项目-T00007918-氮氧传感器-签批.pdf` (sor_baic_core): `84.62%`, checks `11/13`
- `Q-JLY J7111029E-2024-汽车电气和电子零部件通用技术规范.pdf` (q_jly_core): `100.0%`, checks `13/13`
- `QC_T_1067_1-09_01_2017-zh-Aktuell_-_Gueltig-Standard_-_.pdf` (qc_t_1067_monitor): `100.0%`, checks `13/13`
- `eSigned_EMC_88033918_FIL_000_AA 4.pdf` (esigned_emc_88033918): `100.0%`, checks `13/13`
- `eSigned_ET_88033919_FIL_000_AA 3.pdf` (esigned_et_88033919): `100.0%`, checks `13/13`

## Top Issues
- `SOR-N82KS-E01项目-T00007918-氮氧传感器-签批.pdf` | `required_code_missing` | `E:2` | required scoped code missing
- `SOR-N82KS-E01项目-T00007918-氮氧传感器-签批.pdf` | `required_title_missing_code` | `E:2` | title check missing code; need contains '系统职责'
- `产品设计规格说明书（PDS）-DE全主动减振器总成-V1.0-20251127.pdf` | `section_count_too_low` | `` | sections=65 min=85
- `产品设计规格说明书（PDS）-DE全主动减振器总成-V1.0-20251127.pdf` | `requirement_count_too_low` | `` | requirements=54 min=60
- `产品设计规格说明书（PDS）-DE全主动减振器总成-V1.0-20251127.pdf` | `section_count_drift` | `` | sections=65 expected=95 drift=30
- `产品设计规格说明书（PDS）-DE全主动减振器总成-V1.0-20251127.pdf` | `requirement_count_drift` | `` | requirements=54 expected=69 drift=15
- `产品设计规格说明书（PDS）-DE前后油泵总成-V1.1-20251215.pdf` | `section_count_too_low` | `` | sections=108 min=145
- `产品设计规格说明书（PDS）-DE前后油泵总成-V1.1-20251215.pdf` | `requirement_count_too_low` | `` | requirements=101 min=130
- `产品设计规格说明书（PDS）-DE前后油泵总成-V1.1-20251215.pdf` | `section_count_drift` | `` | sections=108 expected=159 drift=51
- `产品设计规格说明书（PDS）-DE前后油泵总成-V1.1-20251215.pdf` | `requirement_count_drift` | `` | requirements=101 expected=140 drift=39
- `产品设计规格说明书（PDS）-DE前后油泵总成-V1.1-20251215.pdf` | `required_title_mismatch` | `1` | title '概述' does not contain '低温存储'
- `产品设计规格说明书（PDS）-DE前后油泵总成-V1.1-20251215.pdf` | `required_title_mismatch` | `2` | title '供应商能力要求及设计责任' does not contain '高温存储'

## Missing Profiles
- None

## Skipped Docs
- None
