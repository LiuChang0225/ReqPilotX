# Phase-0 Baseline Report

## 1) Sampling Overview
- Source PDF dir: `/Users/liuchang/Downloads/Test pdf`
- Target sample pages: `100`
- Actual sample pages: `100`
- Auto predicted items: `152`

- `PRINT_21033420PEF16C02-JYA-0226~PEF16C02-电涡流_SOR~A.pdf`: total `30` pages, sampled `10` pages
- `Q-JLY J7111029E-2024-汽车电气和电子零部件通用技术规范.pdf`: total `137` pages, sampled `43` pages
- `QC_T_1067_1-09_01_2017-zh-Aktuell_-_Gueltig-Standard_-_.pdf`: total `60` pages, sampled `19` pages
- `SOR-N82KS-E01项目-T00007918-氮氧传感器-签批.pdf`: total `35` pages, sampled `11` pages
- `eSigned_EMC_88033918_FIL_000_AA 4.pdf`: total `21` pages, sampled `8` pages
- `eSigned_ET_88033919_FIL_000_AA 3.pdf`: total `28` pages, sampled `9` pages

## 2) Baseline Metrics (Current Parser + Extractor)
- Parse coverage (`parse_has_text=1`): `99.00%`
- Avg raw noise ratio: `29.99%`
- Median raw noise ratio: `25.59%`
- Pages with extracted items: `50/100`
- Avg extracted items per page: `1.52`
- Duplicate item rate: `5.26%`
- Short item rate (`len<20`): `0.66%`
- Auto type distribution: `FUNCTIONAL:10, INTERFACE:2, OTHER:40, PERFORMANCE:70, REGULATION:14, TEST:16`
- Auto priority distribution: `HIGH:84, LOW:49, MEDIUM:19`

## 3) Supervised Metrics (After Manual Annotation)
- Labeled predicted items: `8`
- Precision: `62.50%`
- Recall: `PENDING` (method: `not_available`)
- Split accuracy: `0.00%`
- Type accuracy: `50.00%`

## 4) Top False-Positive Patterns
- `other`: 3

## 5) Interpretation
- If Precision/Recall are `PENDING`, complete manual labels in:
  - `phase0_page_annotation_template.csv`
  - `phase0_item_annotation_template.csv`
- For strict Recall, append missed items with `source_type=missed_gold`.
- Use this report as the gate for Parse/Extract iteration acceptance.
