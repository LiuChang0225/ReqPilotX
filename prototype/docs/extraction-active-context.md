# Extraction Active Context

更新时间：2026-04-19 09:45 CST
分支：`codex/extraction-quality-loop`

## 分支目的

- 当前分支仍是 extraction 回归修复入口；最新 mixed alpha/numeric clause 修复已经进入 `main`，本分支保留为下一轮 extraction 调优与 live 复核入口。
- 主线代码状态以 `main` 为准；当前分支只继续承接新的 extraction 回归和 focused diagnose。

## 当前样本

- `Q-SK J02.311-2025_UDS on IP、CAN、CANFD诊断技术规范.pdf`
- `【机密】V32&K89_CTS_ASC_V1.1.0_20250630 (1).pdf`
- `Q-SKJ00.14-2025_汽车电子控制单元版本管理编码规则【机密】.pdf`
- `Q-SK J02.385-2024_总线网络E2E通信保护设计要求 (1).pdf`

## 最近已修

- `prototype/app/services/extraction_preview.py` 已收口：
  - mixed alpha/numeric clause 现在会先拆 numeric row 内嵌的 alpha tail，再把父节 inline alpha 前缀 canonicalize 成同序 alpha row，并对同 anchor 重复 alpha label 去重。
  - 修复生成的 alpha row 当前记为 `HYBRID`，不再抬高 requirement benchmark；显式 visual asset repair 也已放宽到 `HYBRID`。
- `prototype/app/services/extractor.py` 已收口：
  - 显式 `图/表` 标题不再被 title-only main-body recovery 误恢复成 `1.1` 这类伪 child section。
- `J02.311` focused rebuild 已确认：
  - `section 1` 不再生成 `1.1`，`c）` 已重新挂回 `图1平台需求与ECU 需求的区别` source snapshot。
  - `5.11.3` 仍保持 inline alpha，不会被无故拆成独立行。
  - `6.2.2` 已稳定为 `父节引导 -> a/b/c/d -> 1~6 -> e/f/g/h`。
  - `8.10.4` 已稳定为 `父节引导 -> a -> numeric -> b -> numeric -> ...`。
  - 最新 focused diagnose `fix21` 当前为 `requirements_count = 332`、`requirements_drift = -1`、`residual_embedded_alpha_tail = 0`。
- 长期规则台账保留在 `prototype/docs/seres-mda-rule-fix-context.md`，不再把长账本塞回 active-context。

## 当前保留工件

- `prototype/eval/extraction_batches/seres_mda_20260418_j02311_fix13`
- `prototype/eval/extraction_batches/legacy_benchmark_core_20260418_j02311_fix13`
- `prototype/eval/extraction_diagnose/j02_311_20260419_fix21`

## 当前已知问题

- `J02.311`：`7.3 表10` 仍未升成结构表，当前保持“正确文字 + 原文截图”。
- `J02.311`：focused diagnose 仍保留 `complex_table_clause_binding_drift + parser_low_confidence_hotspot`；最新 residual 为 `requirements_drift = -1`。
- `J02.311`：parser 风险页仍是 `1 / 11 / 12 / 13 / 70 / 89 / 105 / 108 / 112`。
- `CTS_ASC`：`9.3` 仍是 section 级双图输出，尚未做逐图 caption pairing。
- 当前没有新的用户可见主线回归；仓库已去掉旧 export / fidelity / regression / iteration 中间产物，后续只保留当前基线摘要。

## 下一步

- 继续 extraction 复核时，直接基于当前 `5555` live generation 做 spot-check，只盯 `7.3 表10` 与 parser 低置信页。
- 只有在新问题能被复现为通用规则时，才继续更新 `seres-mda-rule-fix-context.md` 和 retained baseline。
