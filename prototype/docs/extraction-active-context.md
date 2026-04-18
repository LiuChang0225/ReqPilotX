# Extraction Active Context

更新时间：2026-04-19 02:06 CST
分支：`codex/extraction-quality-loop`

## 分支目的

- 当前分支已作为“已合入 `main` 的 extraction 质量基线”保留，用于回滚、对照和下一轮 extraction 回归修复入口。
- 主线代码状态以 `main` 为准；只有出现新 extraction 问题时，再回到本分支继续推进。

## 当前样本

- `Q-SK J02.311-2025_UDS on IP、CAN、CANFD诊断技术规范.pdf`
- `【机密】V32&K89_CTS_ASC_V1.1.0_20250630 (1).pdf`
- `Q-SKJ00.14-2025_汽车电子控制单元版本管理编码规则【机密】.pdf`
- `Q-SK J02.385-2024_总线网络E2E通信保护设计要求 (1).pdf`

## 最近已修

- `preview_generation_id` 已贯通 `prototype/app/db.py`、`prototype/app/main.py`、`prototype/app/services/preview_cache.py`、`prototype/app/services/exporter.py`、`prototype/app/static/app.js`，review/preview/export 统一只认当前 generation 资产。
- `prototype/app/services/extractor.py` 已收口：
  - simple scoped ordered item 不再长成 synthetic section
  - version/reference fake clause 被抑制
  - title-only table / flowchart spillover 不再抢占后续 explicit same-code header
- `prototype/app/services/extraction_preview.py` 已收口：
  - repeated numeric local clause repair 改成按 sequence/position 修复
  - `J02.311` focused residual 从 `requirements_drift = 25` 降到 `17`
- 长期规则台账保留在 `prototype/docs/seres-mda-rule-fix-context.md`，不再把长账本塞回 active-context。

## 当前保留工件

- `prototype/eval/extraction_batches/seres_mda_20260418_j02311_fix13`
- `prototype/eval/extraction_batches/legacy_benchmark_core_20260418_j02311_fix13`
- `prototype/eval/extraction_diagnose/j02_311_20260418_fix13`
- `prototype/eval/extraction_diagnose/j02_311_20260418_fix14`
- `prototype/eval/extraction_diagnose/j02_311_20260419_fix15`
- `prototype/storage/exports/seres_mda_batch_export_report_20260417_163356.json`

## 当前已知问题

- `J02.311`：`7.3 表10` 仍未升成结构表，当前保持“正确文字 + 原文截图”。
- `J02.311`：focused diagnose 仍保留 `complex_table_clause_binding_drift + parser_low_confidence_hotspot`；最新 residual 为 `requirements_drift = 17`。
- `CTS_ASC`：`9.3` 仍是 section 级双图输出，尚未做逐图 caption pairing。
- 当前没有新的用户可见主线回归；剩余更多是机器侧稳健性债务。

## 下一步

- 如需继续 extraction 调优，先在本分支重启 `5555` 后复核 live preview，再决定是否补 full batch rerun。
- 只有在新问题能被复现为通用规则时，才继续更新 `seres-mda-rule-fix-context.md` 和 retained baseline。
