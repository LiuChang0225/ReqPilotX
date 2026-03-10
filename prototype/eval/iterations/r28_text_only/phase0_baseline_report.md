# Phase-0 Baseline Report

## 1) Sampling Overview
- Source PDF dir: `/Users/liuchang/Downloads/Test pdf`
- Target sample pages: `100`
- Actual sample pages: `100`
- Auto predicted items: `480`

- `Q-JLY J7111029E-2024-汽车电气和电子零部件通用技术规范.pdf`: total `137` pages, sampled `49` pages
- `QC_T_1067_1-09_01_2017-zh-Aktuell_-_Gueltig-Standard_-_.pdf`: total `60` pages, sampled `21` pages
- `SOR-N82KS-E01项目-T00007918-氮氧传感器-签批.pdf`: total `35` pages, sampled `12` pages
- `eSigned_EMC_88033918_FIL_000_AA 4.pdf`: total `21` pages, sampled `8` pages
- `eSigned_ET_88033919_FIL_000_AA 3.pdf`: total `28` pages, sampled `10` pages

## 2) Baseline Metrics (Current Parser + Extractor)
- Parse coverage (`parse_has_text=1`): `100.00%`
- Avg raw noise ratio: `24.31%`
- Median raw noise ratio: `21.39%`
- Pages with extracted items: `93/100`
- Avg extracted items per page: `4.80`
- Duplicate item rate: `37.50%`
- Short item rate (`len<20`): `0.00%`
- Auto type distribution: `FUNCTIONAL:3, OTHER:95, PERFORMANCE:179, REGULATION:23, TEST:180`
- Auto priority distribution: `HIGH:118, LOW:346, MEDIUM:16`

## 3) Supervised Metrics (After Manual Annotation)
- Labeled predicted items: `7`
- Precision: `100.00%`
- Recall: `PENDING` (method: `not_available`)
- Split accuracy: `100.00%` (split-labeled rows: `2`)
- Type accuracy: `42.86%` (type-labeled rows: `7`)

## 4) Top False-Positive Patterns
- N/A

## 5) Interpretation
- If Precision/Recall are `PENDING`, complete manual labels in:
  - `phase0_page_annotation_template.csv`
  - `phase0_item_annotation_template.csv`
- For strict Recall, append missed items with `source_type=missed_gold`.
- Use this report as the gate for Parse/Extract iteration acceptance.
