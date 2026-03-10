# Phase-0 Annotation Guide

## 1. Page-Level (`phase0_page_annotation_template.csv`)
- `is_page_readable`: `1` readable, `0` unreadable (scan/garbled).
- `gold_total_requirements`: this page's true requirement count.
- `gold_noise_lines_pct`: estimated noise line percentage (0-100).
- `notes`: record special issue (table-only, watermark-heavy, etc.).

## 2. Item-Level (`phase0_item_annotation_template.csv`)
- `source_type=predicted` rows are auto-extracted candidates.
- `is_true_requirement`: `1` true requirement, `0` false positive.
- `is_split_correct`: `1` if one atomic requirement; `0` if over/under split.
- `is_type_correct`: `1` if type classification is correct.
- `gold_requirement_text`: corrected requirement text when needed.
- `gold_requirement_type`: corrected type when needed.
- `error_tags`: comma-separated tags such as
  `header_footer_noise, table_parse_error, heading_leakage, broken_ocr, over_split, under_split`.

## 3. Optional Recall Support
When you find missing requirements (model did not extract), append new rows with:
- `source_type=missed_gold`
- `auto_requirement_text` empty
- fill `gold_requirement_text` and `gold_requirement_type`

This enables strict Recall computation in baseline reports.
