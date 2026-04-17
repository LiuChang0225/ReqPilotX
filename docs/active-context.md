# Active Context

更新时间：2026-04-17 16:03 CST  
适用仓库：`/Users/liuchang/Documents/New project`

长期背景：[`/Users/liuchang/Documents/New project/docs/项目.md`](/Users/liuchang/Documents/New%20project/docs/项目.md)

## 当前目标

- 在 `extraction-quality-loop` 轨道内继续执行单线闭环：`新文档测试 -> 结构审计 -> 问题定位 -> 通用修规则 -> 回归验证 -> 模式沉淀`
- 当前主线从 `CTS_ASC` 大面积缺节/错图，转为“收 residual + 审计其它 SERES 文档”
- 当前新增重点是把 `J00.14 / J02.311 / J02.385` 这类其它文档按“PDF 原页 / preview / diagnose”三边对照补齐

## 活跃分支 / 工作树

- 分支：`codex/extraction-quality-loop`
- worktree：`/Users/liuchang/Documents/New project/worktrees/_active/extraction-quality-loop`
- 当前工作树仍 dirty，含既有本地改动与运行产物：不要 `reset / revert`

## 当前样本

- `【机密】V32&K89_CTS_ASC_V1.1.0_20250630 (1).pdf`
- `Q-SK J02.311-2025_UDS on IP、CAN、CANFD诊断技术规范.pdf`
- `Q-SKJ00.14-2025_汽车电子控制单元版本管理编码规则【机密】.pdf`
- `Q-SK J02.385-2024_总线网络E2E通信保护设计要求 (1).pdf`
- `赛力斯UDS刷写规范 安全签名算法要求V1.1_20250410【AAA】.pdf`

## 最近已修

- `prototype/app/services/extraction_preview.py`
  - ad hoc / 外链 PDF 的默认 `table_snapshots` 路径现在优先落到 staged PDF 同目录，若不可写则回退到 workspace storage，避免向 `Downloads/SERES MDA/table_snapshots` 写入时报 `PermissionError`
  - simple scoped ordered item 不再物化成 standalone synthetic section / heading；`J02.311` 的 `8.10.4` 之类章节现在保留 `1）/2）/3）` requirement 标签，但不会再长出伪 section tree 节点
- `prototype/app/services/extractor.py`
  - 版本示例值、章节引用值不再被拆成 fake local clause；`J00.14` 的 `SW:AC.1.16.23 / HW:C.1.3` 和 `J02.311` 的 `见 7.2 章节` 已恢复正常
  - `Profile 1A` 不再被 measurement-title 过滤；`J02.385` 的 `5.2 Profile 1A` 已恢复为真 heading
- 真实核对已确认：
  - `J00.14`：fake version clause 已消失
  - `J02.311`：`8.10.4` 下不再有 synthetic child section / heading，列表项标签仍保留
  - `J02.385`：`5.2 Profile 1A` 已从 `5.1` 中拆回
  - `CTS_ASC`：`4.2.2.4 / 4.4.2 / 5.4 / 9.1 / 9.3` 在新规则下未被误伤

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
  - 合计 `93 tests / OK`
- 本轮 targeted real-preview rebuild：
  - `J02.311`：`sections_count = 345`，`8.10.4` 下 `section_tree / complete entries` 均不再生成伪 heading
  - `CTS_ASC`：`requirements_count = 41`，`raw_requirement_rows_count = 43`，关键 section 保持稳定

## 当前 blockers

- `CTS_ASC`
  - page `29` 仍是 `parser_low_confidence_hotspot`
  - `4.4.2` complex-table binding 仍需继续收口
  - `9.3` 仍是 section 级双图输出，尚未做 `MPU_F / MPU_R` 逐图配对
- `SERES`
  - `fix10` 是本轮前一版完整批跑；simple scoped ordered item 显示规则落地后，还没做一轮新的 full batch baseline
  - 未建 profile 的文档仍会出现 `profile_unmatched`

## 下一步

- 先继续收 `CTS_ASC` residual：page `29` parser 选型、`4.4.2` complex-table binding、评估 `9.3` 是否继续做 caption-image pairing
- 再决定是否用当前规则集重跑一轮新的 SERES full batch，升格为新的 extraction baseline
- 若继续扩 profile，优先考虑其余高频 SERES 文档而不是先追 diagnose 噪音

## 活跃端口

- review 服务：`http://127.0.0.1:5555/workspace?stage=review`
- 当前 `5555` 已在新代码上重启；`/api/health = 200`，`/workspace?stage=review = 200`
