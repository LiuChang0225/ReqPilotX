# Fidelity Benchmark

- PDF dir: `/Users/liuchang/Documents/New project/worktrees/_active/extraction-quality-loop/prototype/eval/extraction_batches/extraction_quality_loop_20260411_singleline_audit/staged_docs`
- Target docs: `4`
- Checks: `80/92`
- Score: `86.96%`
- Failed docs: `3`
- Overall pass: `False`

## Document Scores
- `SOR-N82KS-E01项目-T00007918-氮氧传感器-签批.pdf` (sor_baic_core): `78.26%`, checks `18/23`
- `产品设计规格说明书PDS-DE全主动减振器总成-V1.0-20251127.pdf` (pds_de_active_damper_v1_0): `78.26%`, checks `18/23`
- `Q-JLYJ7111029E-2024-汽车电气和电子零部件通用技术规范.pdf` (q_jly_core): `91.3%`, checks `21/23`
- `产品设计规格说明书PDS-DE前后油泵总成-V1.1-20251215.pdf` (pds_de_oil_pump_v1_1): `100.0%`, checks `23/23`

## Top Issues
- `Q-JLYJ7111029E-2024-汽车电气和电子零部件通用技术规范.pdf` | `requirement_count_drift` | `` | requirements=451 expected=398 drift=53
- `Q-JLYJ7111029E-2024-汽车电气和电子零部件通用技术规范.pdf` | `section_consistency_issue_limit` | `` | consistency_issues=8 max=3
- `SOR-N82KS-E01项目-T00007918-氮氧传感器-签批.pdf` | `requirement_count_too_low` | `` | requirements=172 min=180
- `SOR-N82KS-E01项目-T00007918-氮氧传感器-签批.pdf` | `requirement_count_drift` | `` | requirements=172 expected=219 drift=47
- `SOR-N82KS-E01项目-T00007918-氮氧传感器-签批.pdf` | `section_consistency_issue_limit` | `` | consistency_issues=2 max=1
- `SOR-N82KS-E01项目-T00007918-氮氧传感器-签批.pdf` | `required_code_missing` | `F:1` | required scoped code missing
- `SOR-N82KS-E01项目-T00007918-氮氧传感器-签批.pdf` | `required_code_missing` | `G:1` | required scoped code missing
- `SOR-N82KS-E01项目-T00007918-氮氧传感器-签批.pdf` | `required_title_mismatch` | `E:2` | title '线束长度： TBD （根据实车布置进行调整）' does not contain '系统职责'
- `产品设计规格说明书PDS-DE全主动减振器总成-V1.0-20251127.pdf` | `required_title_mismatch` | `4` | title '前蓄能器布置及要求' does not contain '标准法规要求'
- `产品设计规格说明书PDS-DE全主动减振器总成-V1.0-20251127.pdf` | `required_title_mismatch` | `5` | title '工作环境及寿命要求' does not contain '可靠性设计与验证需求'
- `产品设计规格说明书PDS-DE全主动减振器总成-V1.0-20251127.pdf` | `required_title_mismatch` | `6` | title '整车载荷输入，待定' does not contain '其他'
- `产品设计规格说明书PDS-DE全主动减振器总成-V1.0-20251127.pdf` | `required_requirement_text_mismatch` | `3.2.2` | requirement text does not contain '3）减振器阻尼力基础要求'
- `产品设计规格说明书PDS-DE全主动减振器总成-V1.0-20251127.pdf` | `required_requirement_text_mismatch` | `3.2.2` | requirement text does not contain '0.026m/s-1m/s阻尼力需线性变化'
- `产品设计规格说明书PDS-DE全主动减振器总成-V1.0-20251127.pdf` | `required_requirement_text_mismatch` | `3.2.2` | requirement text does not contain '4）前蓄能器布置及要求'
- `产品设计规格说明书PDS-DE全主动减振器总成-V1.0-20251127.pdf` | `required_requirement_text_mismatch` | `3.2.2` | requirement text does not contain '5）工作环境及寿命要求'
- `产品设计规格说明书PDS-DE全主动减振器总成-V1.0-20251127.pdf` | `required_requirement_text_mismatch` | `3.2.2` | requirement text does not contain '6）整车载荷输入'
- `产品设计规格说明书PDS-DE全主动减振器总成-V1.0-20251127.pdf` | `required_requirement_text_mismatch` | `3.2.2` | requirement text does not contain '7）DVP试验要求'
- `产品设计规格说明书PDS-DE全主动减振器总成-V1.0-20251127.pdf` | `required_requirement_text_mismatch` | `3.2.2` | requirement text does not contain '7.3.1 ）外观磕伤、划伤标准'
- `产品设计规格说明书PDS-DE全主动减振器总成-V1.0-20251127.pdf` | `required_requirement_text_mismatch` | `3.2.2` | requirement text does not contain '9）产品永久性标识应符合Q/JQ 158、Q/JQ 159'
- `产品设计规格说明书PDS-DE全主动减振器总成-V1.0-20251127.pdf` | `required_requirement_text_mismatch` | `3.2.2` | requirement text does not contain '10）三新件及首发技术风险'
- `产品设计规格说明书PDS-DE全主动减振器总成-V1.0-20251127.pdf` | `required_requirement_text_mismatch` | `3.2.2` | requirement text does not contain '10.1 ）是否三新件'
- `产品设计规格说明书PDS-DE全主动减振器总成-V1.0-20251127.pdf` | `required_requirement_text_mismatch` | `3.2.2` | requirement text does not contain '[ √ ]新技术'
- `产品设计规格说明书PDS-DE全主动减振器总成-V1.0-20251127.pdf` | `required_requirement_text_mismatch` | `3.2.2` | requirement text does not contain '[ ]新材料'
- `产品设计规格说明书PDS-DE全主动减振器总成-V1.0-20251127.pdf` | `required_requirement_text_mismatch` | `3.2.2` | requirement text does not contain '[ ]新工艺'
- `产品设计规格说明书PDS-DE全主动减振器总成-V1.0-20251127.pdf` | `required_requirement_text_mismatch` | `3.2.2` | requirement text does not contain '11.1 ） 低压电器件防护等级要求'
- `产品设计规格说明书PDS-DE全主动减振器总成-V1.0-20251127.pdf` | `required_requirement_text_mismatch` | `3.2.2` | requirement text does not contain '11.2 ） 涉水要求'
- `产品设计规格说明书PDS-DE全主动减振器总成-V1.0-20251127.pdf` | `required_requirement_text_mismatch` | `3.2.2` | requirement text does not contain '11.3 ） 冰冻要求'
- `产品设计规格说明书PDS-DE全主动减振器总成-V1.0-20251127.pdf` | `required_requirement_text_mismatch` | `3.2.2` | requirement text does not contain '12、零部件需满足GB/T 26988'
- `产品设计规格说明书PDS-DE全主动减振器总成-V1.0-20251127.pdf` | `required_requirement_text_mismatch` | `3.2.2` | requirement text does not contain '13、试验大纲内涉及强化耐久项目'
- `产品设计规格说明书PDS-DE全主动减振器总成-V1.0-20251127.pdf` | `required_requirement_text_mismatch` | `3.2.2` | requirement text does not contain '14、电气部件低压连接器需满足Q/JQ4009'
- `产品设计规格说明书PDS-DE全主动减振器总成-V1.0-20251127.pdf` | `required_requirement_text_mismatch` | `3.2.2` | requirement text does not contain '15、供应商应配合完成减振器能耗优化'
- `产品设计规格说明书PDS-DE全主动减振器总成-V1.0-20251127.pdf` | `required_requirement_order_missing_snippet` | `3.2.2` | ordered snippet not found: '9）产品永久性标识应符合Q/JQ 158、Q/JQ 159'
- `产品设计规格说明书PDS-DE全主动减振器总成-V1.0-20251127.pdf` | `required_table_row_snippet_missing` | `3.2.2` | table row snippet not found: '16 法规'
- `产品设计规格说明书PDS-DE全主动减振器总成-V1.0-20251127.pdf` | `forbidden_cross_section_source_missing` | `3.2.2` | source does not contain snippet: '7.3.1 ）外观磕伤、划伤标准'

## Missing Profiles
- `esigned_emc_88033918`
- `esigned_et_88033919`
- `qc_t_1067_monitor`

## Skipped Docs
- None
