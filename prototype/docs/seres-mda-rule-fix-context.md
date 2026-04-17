# SERES MDA Rule Fix Context

更新时间：2026-04-17 16:03 CST  
分支：`codex/extraction-quality-loop`

## 当前目标

- 持续把 `/Users/liuchang/Downloads/SERES MDA` 这组客户文档作为 extraction quality 主批次
- 记录这轮已经落地的通用规则修复、真实核对结论、以及剩余问题

## 本轮新增落地

- `extraction_preview`
  - ad hoc / 外链 PDF 的默认 snapshot root 现在优先落到 staged PDF 同目录，不可写时回退到 workspace storage，修掉 `Downloads/SERES MDA/table_snapshots` 权限失败
  - simple scoped ordered item 不再物化成 standalone synthetic section / heading，普通 `1）/2）/3）` 现在仅保留 requirement 标签
- `extractor`
  - 版本示例值、章节引用值不再拆成 fake local clause
  - `Profile 1A` heading 不再被 measurement-title 过滤

## 真实核对结论

- `Q-SKJ00.14`
  - `SW:AC.1.16.23 / HW:C.1.3` 不再被拆成伪 local clause
  - `4.1.5 / 5.1` 的 fake requirement 行已消失
- `Q-SK J02.311`
  - `7.6.3.2` 的 `见 7.2 章节` 不再被拆成伪 clause
  - `8.10.4` 下不再生成 `1）/2）/3）` synthetic child section / heading
  - requirement 标签仍保留，例如 `1）/2）/3）`
- `Q-SK J02.385`
  - `5.2 Profile 1A` 已恢复为真 heading，不再被吞进 `5.1`
- `CTS_ASC`
  - 新规则未误伤 `4.2.2.4 / 4.4.2 / 5.4 / 9.1 / 9.3`

## 当前结果

- 最新完整 SERES 批次：`prototype/eval/extraction_batches/seres_mda_20260417_fix10`
  - `issue_rollup = 0`
  - `quality_gate = PASS`
  - `fidelity_gate = PASS`
  - `regression-suite = PASS`
  - `oil-pump-benchmark = FAIL` 仍是既有独立问题
- 本轮聚焦回归：
  - `python3 -m unittest prototype.tests.test_extraction_preview -q`
  - `python3 -m unittest prototype.tests.test_extractor_sections -q`
  - `93 tests / OK`
- 本轮 targeted rebuild：
  - `J02.311`：`sections_count = 345`，`8.10.4` 下无 synthetic child section / heading
  - `CTS_ASC`：`requirements_count = 41`，`raw_requirement_rows_count = 43`

## 当前残余

- `CTS_ASC`
  - page `29` 仍有 `parser_low_confidence_hotspot`
  - `4.4.2` complex-table binding 仍需继续核实
  - `9.3` 仍未做到逐图 caption pairing
- `SERES`
  - `fix10` 仍是本轮前一版完整批跑；simple scoped ordered item 显示规则落地后，还未补一轮新的 full batch baseline
  - 未建 profile 文档仍会出现 `profile_unmatched`

## 下一步

- 继续收 `CTS_ASC` residual：page `29` parser 选型、`4.4.2` complex-table binding、评估 `9.3` 是否继续做 caption-image pairing
- 若准备升格当前规则集，先重跑一轮新的 SERES full batch
- 再决定哪些高频 SERES 文档值得补 profile，而不是继续追 diagnose 噪音

## 相关工件

- [SERES batch fix10](/Users/liuchang/Documents/New%20project/worktrees/_active/extraction-quality-loop/prototype/eval/extraction_batches/seres_mda_20260417_fix10/batch_summary.md)
- [J02.311 diagnose fix10](/Users/liuchang/Documents/New%20project/worktrees/_active/extraction-quality-loop/prototype/eval/extraction_batches/seres_mda_20260417_fix10/per_doc/q-skj02-311-2025-udsonipcancanfd诊断技术规范-pdf.diagnosis.json)
- [CTS diagnose fix10](/Users/liuchang/Documents/New%20project/worktrees/_active/extraction-quality-loop/prototype/eval/extraction_batches/seres_mda_20260417_fix10/per_doc/机密-v32-k89-cts-asc-v1-1-0-20250630-1-pdf.diagnosis.json)
- [Branch active context](/Users/liuchang/Documents/New%20project/worktrees/_active/extraction-quality-loop/prototype/docs/extraction-active-context.md)
