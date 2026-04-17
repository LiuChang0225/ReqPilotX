# Extraction Active Context

更新时间：2026-04-17 16:03 CST  
分支：`codex/extraction-quality-loop`

## 分支目的

- 只在 extraction 轨道内做 `parser / extractor / extraction_preview / scripts / tests / eval`
- 继续执行单线闭环：`新文档 -> diagnose -> classify -> patch generic logic -> rerun focused tests -> benchmark-safe promotion`

## 当前样本

- 主复核端口：`5555`
- 主批次：
  - `seres_mda_customer_docs`
  - `legacy_benchmark_core`
- 本轮重点文档：
  - `【机密】V32&K89_CTS_ASC_V1.1.0_20250630 (1).pdf`
  - `Q-SK J02.311-2025_UDS on IP、CAN、CANFD诊断技术规范.pdf`
  - `Q-SKJ00.14-2025_汽车电子控制单元版本管理编码规则【机密】.pdf`
  - `Q-SK J02.385-2024_总线网络E2E通信保护设计要求 (1).pdf`

## 最近已修

- `extraction_preview`
  - ad hoc / 外链 PDF 的 snapshot root 现可安全回退到 workspace storage，修掉 `Downloads/SERES MDA/table_snapshots` 写入失败
  - simple scoped ordered item 不再升成 standalone synthetic section / heading；普通 `1）/2）/3）` 现在只保留 requirement 标签
- `extractor`
  - 版本示例值、章节引用值不再误拆成 fake local clause
  - `Profile 1A` heading 不再被 measurement-title 过滤
- 已真实核对通过：
  - `J00.14`：version example fake clause 消失
  - `J02.311`：`8.10.4` 下 `section_tree / heading entries` 不再生成 `1）/2）/3）` 伪节，但 requirement 标签仍保留
  - `J02.385`：`5.2 Profile 1A` 恢复
  - `CTS_ASC`：`4.2.2.4 / 4.4.2 / 5.4 / 9.1 / 9.3` 在新规则下保持稳定

## 当前结果

- 最新完整批次：`prototype/eval/extraction_batches/seres_mda_20260417_fix10`
  - `issue_rollup = 0`
  - `quality_gate = PASS`
  - `fidelity_gate = PASS`
  - `regression-suite = PASS`
  - `oil-pump-benchmark = FAIL` 仍是既有独立问题
- 本轮聚焦回归：
  - `python3 -m unittest prototype.tests.test_extraction_preview -q`
  - `python3 -m unittest prototype.tests.test_extractor_sections -q`
  - `93 tests / OK`
- targeted rebuild：
  - `J02.311`：`sections_count = 345`
  - `CTS_ASC`：`requirements_count = 41`，`raw_requirement_rows_count = 43`

## 当前已知问题

- `CTS_ASC`
  - page `29` `parser_low_confidence_hotspot`
  - `4.4.2` complex-table binding 仍未完全收口
  - `9.3` 仍是 section 级图片输出，未做逐图 caption pairing
- `SERES`
  - `fix10` 是本轮前一版完整批跑；simple scoped ordered item 显示规则落地后，还未补一轮 full batch
  - 未建 profile 文档仍会有 `profile_unmatched`

## 下一步

- 继续收 `CTS_ASC` residual：page `29` parser 选型、`4.4.2` binding、判断 `9.3` 是否继续做通用 caption-image pairing
- 若准备升基线，先用当前规则集重跑一轮新的 SERES full batch
- 再决定哪些 SERES 文档值得补 profile，而不是继续追噪音 finding

## 运行状态

- `5555` 已在新代码上重启
- `http://127.0.0.1:5555/api/health = 200`
- `http://127.0.0.1:5555/workspace?stage=review = 200`
