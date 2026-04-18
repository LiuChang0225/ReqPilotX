# Active Context

更新时间：2026-04-19 01:19 CST
适用仓库：`/Users/liuchang/Documents/New project`

长期背景：[`/Users/liuchang/Documents/New project/docs/项目.md`](/Users/liuchang/Documents/New%20project/docs/项目.md)

## 当前目标

- 在 `extraction-quality-loop` 轨道内继续执行单线闭环：`诊断 -> 问题卡 -> 通用修规则 -> live review 证据 -> 用户确认`
- 当前主线锁定 `J02.311` 两张残项：
  - `complex_table_clause_binding_drift.requirements_drift` 已从 `25` 降到 `17`
  - `7.3 表10` 仍是“文字 + 原文截图可用，但未升成结构表”
- 当前 live 限制：
  - `5555` 在线项目已 refresh 出新 generation，但当前进程是 `2026-04-18 22:53` 启动的旧内存态；live preview 仍停在 `358`，最新代码级结果以 `fix15` focused diagnose 为准
- `CTS_ASC` 暂退回次优先，只在 `J02.311` 当前轮次向用户汇报后再继续追机器侧 residual
- 用户验收口径保持不变：每个确认问题都要有 `问题 / 根因层 / 通用方案候选 / 前端截图证据`

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

- preview cache / table asset 生命周期新增 generation 收口：
  - `prototype/app/db.py`：`sor_documents / requirements / document_table_assets` 新增 `preview_generation_id`
  - `prototype/app/services/preview_cache.py`：任一 preview rebuild 会先切到新 generation，再清旧 preview cache；cached preview 若 generation 不匹配会直接判 `STALE`
  - `prototype/app/main.py`：review overlay、table-asset fallback、workspace/export requirement 展示只认当前 generation；旧 persisted 图表资产不再跨代回流
  - 对同 source 命中的 requirement，旧 generation 的 `table_refs / content_blocks` 会先剥离，再尽量用当前 preview context 回填，避免“旧图表残留”与“文本状态全丢”两个极端
  - 新增回归覆盖：
    - `test_overlay_review_entries_drops_stale_visual_assets_from_previous_preview_generation`
    - `test_enrich_requirements_with_preview_context_replaces_stale_visual_assets_from_current_preview`
    - `test_table_asset_source_snapshot_does_not_fall_back_to_previous_preview_generation`
- SERES MDA 规则沉淀方式已正式收紧：
  - `prototype/docs/seres-mda-rule-fix-context.md` 现在定义为长期规则台账，必须记录整组文档结构、问题、方案、有效性与残余；active-context 只保留摘要入口
- `prototype/app/services/extraction_preview.py`
  - ad hoc / 外链 PDF 的默认 `table_snapshots` 路径现在优先落到 staged PDF 同目录，若不可写则回退到 workspace storage，避免向 `Downloads/SERES MDA/table_snapshots` 写入时报 `PermissionError`
  - simple scoped ordered item 不再物化成 standalone synthetic section / heading；`J02.311` 的 `8.10.4` 之类章节现在保留 `1）/2）/3）` requirement 标签，但不会再长出伪 section tree 节点
- `prototype/app/services/extractor.py`
  - 版本示例值、章节引用值不再被拆成 fake local clause；`J00.14` 的 `SW:AC.1.16.23 / HW:C.1.3` 和 `J02.311` 的 `见 7.2 章节` 已恢复正常
  - `Profile 1A` 不再被 measurement-title 过滤；`J02.385` 的 `5.2 Profile 1A` 已恢复为真 heading
  - title-only table / flowchart spillover 现在会让位于近距离 explicit 同 code header；`J02.311` 的 `7.3` 不再被 `表10 连接器PIN 脚定义` 顶替，`8.6` 不再被 `子功能是否支持` 顶替
  - 新增 focused 覆盖：
    - `test_table_spillover_title_does_not_preempt_explicit_same_code_header`
    - `test_flowchart_node_title_does_not_preempt_explicit_same_code_header`
- 真实核对已确认：
  - `J00.14`：fake version clause 已消失
  - `J02.311`：`8.10.4` 下不再有 synthetic child section / heading，列表项标签仍保留
  - `J02.385`：`5.2 Profile 1A` 已从 `5.1` 中拆回
  - `CTS_ASC`：`4.2.2.4 / 4.4.2 / 5.4 / 9.1 / 9.3` 在新规则下未被误伤
  - `J02.311`：`5555` live review 当前已确认
    - `7.3 -> 零部件诊断规范`
    - `8.6 -> 诊断会话说明`
    - 修前/修后证据：`output/playwright/seres-evidence/j02311_fix14_before`、`output/playwright/seres-evidence/j02311_fix14_after`
- `J02.311 requirements_drift = 25` 的 residual root cause 已进一步收窄：
  - `_repair_preview_clause_rows_from_pdf(...)` 之前会把同一父节下重复的 numeric local clause 用 `{label: text}` 覆盖，并把 alpha clause 挂到错误 numeric anchor
  - 当前 worktree 已改成 sequence-based numeric consume + position-based alpha insertion
  - `prototype/tests/test_extraction_preview.py` 已补回显式视觉 / asset dedupe / wrapped clause / repeated numeric 相关回归，当前 `44 tests / OK`
- `CTS_ASC` 已启动真实前端截图验收第一批：
  - 证据目录：`output/playwright/seres-evidence/cts_batch1`、`output/playwright/seres-evidence/cts_batch2`
  - 已拿到并由用户在真实前端确认：`1.1 / 1.2 / 1.3 / 2 / 2.1` 全部正确
  - `4.1.2.2` 当前结论更新为：结构与文本已恢复，但表格仍主要以 `snapshot_only` 视觉 fallback 呈现，尚未恢复为可结构化读取的 table rows
  - 第二批已补到可审截图：
    - `4.1.2.2`：`cts_4_1_2_2_fullpage_seq.png`，确认 `HEADING + REQUIREMENT` 已恢复；残余仍是 structured table 未恢复
    - `4.2.2.3`：`cts_4_2_2_3_refresh.png`，图片已回挂
    - `4.2.2.4`：`cts_4_2_2_4_refresh.png`，当前 preview 为 `1` 张推荐截图 + `1` 张 source snapshot，未再见首轮那种重复大图
    - `4.4.2`：`cts_4_4_2_fullpage_seq.png`，当前 filtered preview 不再混入 `4.4.1` 错图；但 complex-table binding residual 仍在
    - `5.4`：`cts_5_4_refresh.png`，`环境温度≤110℃。` 已在前端可见
    - `6 / 7 / 8`：`cts_6_fullpage_seq.png`、`cts_7_refresh.png`、`cts_8_refresh.png`，heading/正文均已回到 preview
    - `9.1 / 9.3`：`cts_9_1_refresh.png`、`cts_9_3_fullpage_seq.png`，图片已回挂；`9.3` 仍未做逐图 caption pairing
- 本轮新增通用修复已落地但尚待前端复验：
  - `headerless structured table` 支持已补到 `extraction_preview / table_assets / exporter / app.js`
  - 针对 `4.4.2` 这类 local-clause 二列表：
    - 预览重建不再强塞 `ASC_R / 说明` 这种假表头
    - 已存在的 synthetic local-clause header ref 也会在 preview 响应阶段被剥离成无表头结构表
  - 聚焦回归：`python3 -m unittest prototype/tests/test_extraction_preview.py -q` -> `28 tests / OK`

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
- 本轮 `J02.311 fix14` 聚焦回归：
  - `python3 -m unittest prototype.tests.test_extractor_sections.ExtractorSectionConsolidationTest.test_table_title_rows_recover_missing_child_sections prototype.tests.test_extractor_sections.ExtractorSectionConsolidationTest.test_table_spillover_title_does_not_preempt_explicit_same_code_header prototype.tests.test_extractor_sections.ExtractorSectionConsolidationTest.test_flowchart_node_title_does_not_preempt_explicit_same_code_header`
  - `3 tests / OK`
- 本轮 targeted real-preview rebuild：
  - `J02.311`：`sections_count = 345`，`8.10.4` 下 `section_tree / complete entries` 均不再生成伪 heading
  - `CTS_ASC`：`requirements_count = 41`，`raw_requirement_rows_count = 43`，关键 section 保持稳定
- 本轮 `J02.311 fix14` focused artifacts：
  - live preview：`7.3 / 8.6` 标题已纠正
  - 证据：
    - `output/playwright/seres-evidence/j02311_fix14_after/j02311_7_3_after_full.png`
    - `output/playwright/seres-evidence/j02311_fix14_after/j02311_8_6_after_full.png`
  - diagnose：`prototype/eval/extraction_diagnose/j02_311_20260418_fix14`
    - `total_findings = 2`
    - `complex_table_clause_binding_drift.requirements_drift = 25`
    - `parser_low_confidence_hotspot = [1, 11, 12, 13, 70, 89, 105, 108, 112]`
- 本轮 `J02.311 fix15` focused diagnose：
  - 工件：`prototype/eval/extraction_diagnose/j02_311_20260419_fix15`
  - `total_findings = 2`
  - `complex_table_clause_binding_drift.requirements_drift = 17`
  - `requirements_count = 350`，`expected = 333`
  - `parser_low_confidence_hotspot = [1, 11, 12, 13, 70, 89, 105, 108, 112]`
- 本轮 live refresh 验证：
  - 项目 `SERES MDA Batch Export 20260417_163356` 中的 `J02.311` 已刷新到 `preview_generation_id = 813cee56-c0c1-4d95-99d9-c677ff8baa86`
  - 当前 `5555` 刷新后仍返回 `summary.requirements_count = 358`
  - 当前判断：在线服务尚未承载到本轮 `fix15` 内存态；live UI 复核需在重启 `5555` 后重做
- 本轮缓存代际回归：
  - `PYTHONPATH=prototype python3 -m unittest prototype.tests.test_unified_workspace -q` -> `109 tests / OK`
  - `PYTHONPATH=prototype python3 -m unittest prototype.tests.test_extractor_sections -q` -> `66 tests / OK`
  - `python3 -m unittest prototype.tests.test_extraction_preview -q` -> `44 tests / OK`

## 当前 blockers

- `J02.311`
  - `7.3 / 8.6` 的 section ownership 漂移工程侧已修并已拿到 live screenshot；当前只剩用户确认，不是当前代码调试焦点
  - `complex_table_clause_binding_drift.requirements_drift` 已从 `25` 降到 `17`；residual 仍集中在 preview clause repair / complex-table rewrite ordering
  - `7.3 表10` 当前仍是“文字 + 原文截图可用，但未升成结构表”
  - `1 / 11 / 12 / 13 / 70 / 89 / 105 / 108 / 112` 仍统一记为“parser 风险，无当前可见回归”
  - 当前 `5555` live refresh 仍给出 `358` 条 requirement，不能直接拿来代表 `fix15`；若用户要最新前端证据，需先重启服务
- `CTS_ASC`
  - 真实前端截图链路已基本稳定到“单会话串行 + 必要时 full-page”工作流，但 chat 内联图对用户端不可见，汇报时仍需给可点击文件路径
  - page `29` 仍是 `parser_low_confidence_hotspot`
  - `4.4.2` complex-table binding 仍需继续收口
  - `4.4.2` 的“假表头”逻辑虽已改掉，但这轮服务重启后 Playwright 会话卡住，尚未拿到新的前端验证截图，不能提前报通过
  - `9.3` 仍是 section 级双图输出，尚未做 `MPU_F / MPU_R` 逐图配对
- `SERES`
  - `fix10` 是本轮前一版完整批跑；simple scoped ordered item 显示规则落地后，还没做一轮新的 full batch baseline
  - 未建 profile 的文档仍会出现 `profile_unmatched`

## 下一步

- 先按 `问题 / 根因层 / 通用方案候选 / 前端截图证据` 向用户汇报这轮 `J02.311 fix15` 结果，明确 `25 -> 17`
- 若用户要求最新 live UI 证据，先重启 `5555` 再对 `J02.311` 做一次 refresh-preview，确认前端是否也从 `358` 对齐到 `350`
- 若 residual 仍在，再决定是继续追 `7.3 表10` 的结构化恢复，还是回到通用层的 complex-table rewrite ordering / dedupe / requirement binding
- `J02.311` 收口后，再回到 `CTS_ASC` residual：page `29` parser 选型、`4.4.2` complex-table binding、`9.3` caption-image pairing

## 活跃端口

- review 服务：`http://127.0.0.1:5555/workspace?stage=review`
- 当前 `5555` 在线，但进程启动时间仍是 `2026-04-18 22:53`；`/api/health = 200`，`/workspace?stage=review = 200`
