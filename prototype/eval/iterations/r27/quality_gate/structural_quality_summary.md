# Structural Quality Gate

- PDF dir: `/Users/liuchang/Downloads/Test pdf`
- Documents: `6`
- Checks passed: `25/26`
- Gate score: `96.15%`

## Document Scores
- `eSigned_EMC_88033918_FIL_000_AA 4.pdf`: score `75.0%`, outline `10`, suspicious `0`, gap alerts `0`
- `PRINT_21033420PEF16C02-JYA-0226~PEF16C02-电涡流_SOR~A.pdf`: score `100.0%`, outline `20`, suspicious `0`, gap alerts `1`
- `Q-JLY J7111029E-2024-汽车电气和电子零部件通用技术规范.pdf`: score `100.0%`, outline `402`, suspicious `3`, gap alerts `0`
- `QC_T_1067_1-09_01_2017-zh-Aktuell_-_Gueltig-Standard_-_.pdf`: score `100.0%`, outline `109`, suspicious `1`, gap alerts `0`
- `SOR-N82KS-E01项目-T00007918-氮氧传感器-签批.pdf`: score `100.0%`, outline `208`, suspicious `5`, gap alerts `0`
- `eSigned_ET_88033919_FIL_000_AA 3.pdf`: score `100.0%`, outline `33`, suspicious `0`, gap alerts `0`

## Top Issues
- `PRINT_21033420PEF16C02-JYA-0226~PEF16C02-电涡流_SOR~A.pdf` | `sibling_gap_alert` | `5.4` | missing=['5.4.8', '5.4.10', '5.4.11', '5.4.12', '5.4.13', '5.4.14', '5.4.15', '5.4.17']
- `Q-JLY J7111029E-2024-汽车电气和电子零部件通用技术规范.pdf` | `suspicious_heading` | `5.1.8.2.2` | table_like_title
- `Q-JLY J7111029E-2024-汽车电气和电子零部件通用技术规范.pdf` | `suspicious_heading` | `5.1.8.2.3` | table_like_title
- `Q-JLY J7111029E-2024-汽车电气和电子零部件通用技术规范.pdf` | `suspicious_heading` | `6` | table_like_title
- `QC_T_1067_1-09_01_2017-zh-Aktuell_-_Gueltig-Standard_-_.pdf` | `suspicious_heading` | `4.5` | table_like_title
- `SOR-N82KS-E01项目-T00007918-氮氧传感器-签批.pdf` | `suspicious_heading` | `2.6` | late_low_major
- `SOR-N82KS-E01项目-T00007918-氮氧传感器-签批.pdf` | `suspicious_heading` | `2.7` | late_low_major
- `SOR-N82KS-E01项目-T00007918-氮氧传感器-签批.pdf` | `suspicious_heading` | `2.8` | late_low_major
- `SOR-N82KS-E01项目-T00007918-氮氧传感器-签批.pdf` | `suspicious_heading` | `1.1.3` | late_low_major
- `SOR-N82KS-E01项目-T00007918-氮氧传感器-签批.pdf` | `suspicious_heading` | `1.1.4` | late_low_major

## Split Confidence
- Point estimate: `100.00%` with `n=8` labeled split rows
- Wilson 95% CI: `67.56% ~ 100.00%`
