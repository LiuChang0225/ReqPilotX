# Extraction Active Context

更新时间：2026-04-13 16:58 CST  
分支：`codex/extraction-quality-loop`

## 分支目的

- 专注拆解质量与批量回归闭环
- 扩展样本集、问题分类法与 benchmark-safe 优化
- 当前分支采用单线闭环：`新文档测试 -> 结构审计 -> 问题定位 -> 通用修规则 -> 回归验证 -> 模式沉淀`
- 当前分支负责 `parser / extractor / extraction_preview / scripts / tests / eval / 当前 track skill` 范围内的审计与 promotion
- 当前不做产品侧 audit job / API / UI 接入

## 当前样本

- 当前 batch：`legacy_benchmark_core`
- 当前关注样本：
  - `Q-JLYJ7111029E-2024-汽车电气和电子零部件通用技术规范.pdf`
  - `SOR-N82KS-E01项目-T00007918-氮氧传感器-签批.pdf`
  - `产品设计规格说明书PDS-DE全主动减振器总成-V1.0-20251127.pdf`
  - `产品设计规格说明书PDS-DE前后油泵总成-V1.1-20251215.pdf`
- 当前主验证端口：`8001`
- 当前关注链路：parser / extractor / extraction preview / batch regression

## 最近已修 / 已确认

- 新增 `prototype/scripts/extraction_batch_report.py`，把 structural gate / fidelity / section split 统一收口到同一 batch 报告
- 新增 `prototype/scripts/extraction_diagnose.py`，对单文档产出 heading skeleton、locality 检查、duplicate scope 检查、page hotspots、diagnosis summary、pattern candidates
- 新增 `prototype/eval/extraction_batches.json`，显式管理核心样本目录与 gate 目标
- 新增 `prototype/eval/extraction_issue_taxonomy.json`，统一 issue taxonomy
- 新增 `prototype/eval/extraction_patterns.json`，作为当前线的 pattern library
- `prototype/eval/quality_gate_profiles.json` 已补齐 `sor_baic_core`、`pds_de_oil_pump_v1_1`
- 已产出 batch 基线工件：`prototype/eval/extraction_batches/extraction_quality_loop_20260409_05`
- 已产出单线闭环 smoke：`prototype/eval/extraction_batches/extraction_quality_loop_20260411_singleline_audit`
  - `diagnosis_summary`: `87` findings / `13` pattern candidates
  - `per_doc` 已覆盖 `Q-JLY / SOR / damper / oil-pump`
- 定向回归已通过：`test_extraction_batch_report`、`test_parser_merge`、`test_extractor_sections`、`test_complex_table_mode`
- 新增回归已通过：`test_extraction_diagnose`
- 已确认油泵 PDS `3.2.2` 下真实存在 `1..12` 与 `6.1 / 6.4 / 7.1 / 8.1.2 / 8.2.5.4 / 11.12` 等本地结构
- 本轮已落地通用修复：
  - `extraction_preview` 支持顶层 local clause regex，并保留 local reset anchor
  - `requirements_count` 改为基于 dedup 后 `complete_entries`
  - fidelity `sections_count` 仅统计 outline section 与 appendix/test-outline scoped synthetic section，主文档 preview-only synthetic local split 不再抬高 benchmark
  - `extractor` synthetic appendix scope 轮转已收敛 `SUB1` raw duplicate scoped code
- 本轮继续落地：
  - `extraction_preview` 为 local clause 增加 scoped identity：`section_key=3.2.2::x`、`parent_section_key`
  - canonical section tree 现在优先按 `parent_section_key` 挂树，`3.2.2` 不再保持 leaf
  - synthetic scoped section 不再从 `section_key` 反推错误的 `:1 / :6.1 / :11.12`，`section_code` 已恢复纯净
  - `fidelity_benchmark` 新增 `required_tree_children` 通用规则，可直接卡局部语义树顺序
  - oil-pump fidelity profile 已加入 `3.2.2` subtree benchmark，并移除两条过期 title 期望
- 浏览器调试链路已确认：
  - `6666` 虽可被 `curl` 访问，但 Chromium 会命中 restricted port 语义，不再作为 UI 验证端口
  - 当前分支服务改由 detached `screen` 常驻在 `8001`
- 本轮新增在线链路确认：
  - 用户页面里看到的油泵 `3.2.2` 乱序，根因不是新规则未生效，而是 `8001` 上仍跑着改动前的 Uvicorn 进程
  - 重启 `8001` 服务并强制 `refresh-preview` 后，在线 `extraction-preview / workspace` 已与当前 worktree 代码对齐
  - 当前在线油泵 preview 已确认存在 `3.2.2::1 / ::6 / ::11.12`，`workspace.summary.order_source=review_overlay` 且 `order_mismatch_count=0`
- 本轮继续落地了 review/display 链路的通用修复：
  - `extraction_preview` 现在只在 chunk 明确识别到 local clause 时允许 `same-code` 提升；`6.投标说明` 中真实 `6）...` 现已变成 `6::6`
  - 同时避免把父节自己的正文误 scope 成假的 `6::6`
  - `main._sort_requirements_for_display()` 已降级为 preview 顺序投影器：matched row 只按 `preview_rank` 排，unmatched row 只做 owner-section 级尾插
  - requirement / heading rows 新增 `owner_section_*` 与 `structural_parent_section_*` 派生字段
  - review 页“章节”列改读 `owner_section_*`，不再用过载的 `parent_section_*` 猜语义；章节总览优先读 `preview.section_tree`
- 本轮已完成真实油泵 PDF 复建与在线 cache 重写：
  - 当前在线 `6.投标说明` 顺序为：`6`（父节 heading）-> 父节说明 requirement -> `6::1` ... `6::5` -> `6::6` -> `6::7` ... `6::10`
  - 用户先前看到的“`6）技术标书中应包含生产工艺部分的描述...` 跑到最前边”已不再复现
- 本轮新增回归已通过：
  - `test_extractor_sections` 新增 same-code `6） -> 6::6` 与 owner / structural label 断言
  - `test_unified_workspace` 新增 `6.投标说明` subtree 顺序与 `review_entries` owner label 断言
- 油泵 PDS 当前已恢复：
  - quality：`10/10`
  - fidelity：`23/23`
  - `sections_count=81`
  - `requirements_count=100`
- 本轮人工复核补充结论：
  - 真实 PDF 中 `3.2.2 产品详细描述` 的正确语义树应直接挂 `1..12`，并继续包含 `6.1..6.7 / 6.6.1..6.6.5 / 7.1..7.5.2 / 8.1.1..8.2.5.4 / 9.1..9.7 / 11.1..11.12`
  - 当前 worktree 已确认 `section_tree` 中 `3.2.2` 正式拥有 `1..12` 子节点，且 `6 / 6.6 / 7 / 7.5 / 8 / 8.1 / 8.2 / 8.2.5 / 9 / 11` 的子链顺序均与真实树一致
  - `oil-pump-benchmark` 在新增 subtree rule 后仍保持 `PASS`
- 已确认 `SUB1` 口径需要区分：
  - synthetic appendix scope 跨页复用本身不一定错
  - 当前真正暴露的是 raw outline 层 `SUB1 + code` 重复过多，`section_instance_key` 只能部分兜底

## 当前已知问题

- `legacy_benchmark_core` 当前为红：
  - structural 仍被 `Q-JLY / SOR` 的 high severity issue 拦截
  - fidelity 当前为 `80/92`
  - `section_split`: `4/4`
- 本轮已重跑 `legacy_benchmark_core` 到既有目录 `prototype/eval/extraction_batches/extraction_quality_loop_20260411_singleline_audit`
  - `batch-report = PASS`
  - `oil-pump-benchmark = PASS`
  - `regression-suite = FAIL`
  - 最新 `batch_summary / diagnosis_summary / per_doc diagnosis / pattern_candidates` 均已刷新
- oil-pump 当前已不再被树结构卡住，但仍有非 gate 残差：
  - `per_doc diagnosis` 仍保留 `local_clause_heading_not_promoted` 与 `complex_table_clause_binding_drift` 两个 pattern candidate
  - `3.2.2::2 / 3.2.2::3 / 3.2.2::6.4` 的表格内容仍存在行级漂移风险，尚未升级成更严格的 row-level benchmark
- 其余样本当前也偏离历史绿色基线：
  - `Q-JLY`: `sections=407 / requirements=451`，当前主问题是 requirement drift 与 consistency drift
  - `SOR-N82KS`: 缺 `F:1 / G:1`，`E:2` 标题漂移，`requirements=172`
  - 减振器 PDS：`sections=245 / requirements=238`，`3.2.2` 文本 / ordered snippet / table row / cross-section binding 仍漂移
- 当前 track 仍不直接承接：
  - 产品内 audit job / API / UI
  - 主运行时 / auth / RAG 逻辑
  - 候选拆解 agent / 独立拆解实验

## 当前验证结论

- 批量回归闭环已建立，控制塔 gate 与 batch 工件已经对齐
- 当前红灯主要是 parser / extractor / preview 的通用结构回归，不是 wrapper / 样本发现 / taxonomy 工具链问题
- 单文档 diagnose 已成为默认入口；batch-report 同轮会补 diagnosis / pattern artifacts，而不是只给 gate 红绿
- 油泵当前已经从“结构 red + fidelity red”收敛到“真实 subtree 对齐 + benchmark green”；后续不再回头重搭油泵树结构，而是补 row-level 内容闸门并把模式推广到剩余样本
- 当前 UI 与 API 的在线复核口径也已回到一致：页面若再看到油泵旧顺序，优先检查服务是否仍是旧进程或 preview 是否仍在 `BUILDING`

## 下一步

- 保持 `3.2.2` subtree benchmark 绿色，先补 oil-pump `2 / 3 / 6.4` 的 row-level 内容 benchmark 与通用修复
- 如果继续收紧 online review 体验，下一步优先评估是否把 `6.投标说明` 父节说明 requirement 从 review 列表单独降噪；但这不是当前 gate blocker
- 下一轮直接转向剩余三类 blocker：
  - `Q-JLY` requirement 膨胀 / consistency drift
  - `SOR-N82KS` `F:1 / G:1` 缺失与 `E:2` 标题漂移
  - 减振器 PDS `3.2.2` complex-table clause binding
- 新文档默认先跑 `prototype/scripts/extraction_diagnose.py`
- 规则修复后固定顺序：聚焦测试 -> 单文档复验 -> `legacy_benchmark_core` -> pattern promotion 记录 -> 更新 context / checkpoint / handoff
- control tower 最新 checkpoint 已写入；下一个窗口直接基于 `extraction_quality_loop_20260411_singleline_audit` 的 `diagnosis_summary / pattern_candidates / per_doc` 继续
- UI 复核入口统一改用 `http://127.0.0.1:8001/workspace`
- 注意：`prototype/scripts/extraction_batch_report.py` 本轮在工件落盘后未自动退出；若下轮复跑遇到同样现象，先读 artifacts，再决定是否手动结束挂起进程
