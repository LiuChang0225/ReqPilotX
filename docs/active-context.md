# Active Context

更新时间：2026-04-12 13:00 CST  
适用仓库：`/Users/liuchang/Documents/New project`

长期背景：[`/Users/liuchang/Documents/New project/docs/项目.md`](/Users/liuchang/Documents/New%20project/docs/项目.md)

## 当前目标

- 在 `extraction-quality-loop` 轨道内建立可复跑的批量拆解回归闭环：样本清单、问题分类法、统一报告、控制塔 gate 同步
- 用统一 batch 明确既有 benchmark 当前状态，重点盯住 `Q-JLY`、`SOR-N82KS`、减振器 PDS、油泵 PDS
- 当前支线已改为单线闭环：`新文档测试 -> 结构审计 -> 问题定位 -> 通用修规则 -> 回归验证 -> 模式沉淀`
- `audit` 不再是单独日常分支，而是 `extraction-quality-loop` 内置子流程；当前不做产品侧 audit job / API / UI 接入

## 活跃分支 / 工作树

- 当前执行支线：`codex/extraction-quality-loop`
- 当前 worktree：`/Users/liuchang/Documents/New project/worktrees/_active/extraction-quality-loop`
- 控制塔运行时：`/Users/liuchang/Documents/New project/.git/reqpilotx-progress/progress_tracker.json`
- 已记录 gate：
  - `batch-report`: `PASS`
  - `regression-suite`: `FAIL`
  - `oil-pump-benchmark`: `PASS`

## 当前样本

- 当前 batch：`legacy_benchmark_core`
- 当前样本目录来源：共享仓库 `prototype/storage/uploads`
- 当前核心样本：
  - `Q-JLYJ7111029E-2024-汽车电气和电子零部件通用技术规范.pdf`
  - `SOR-N82KS-E01项目-T00007918-氮氧传感器-签批.pdf`
  - `产品设计规格说明书PDS-DE全主动减振器总成-V1.0-20251127.pdf`
  - `产品设计规格说明书PDS-DE前后油泵总成-V1.1-20251215.pdf`

## 最近已修 / 已落地

- 新增 `prototype/scripts/extraction_batch_report.py`：
  - 解析 batch manifest
  - 从共享样本目录去重并 stage 逻辑文档
  - 分别跑 structural gate / fidelity / section split
  - 产出统一 `batch_summary / document_rollup / issue_rollup`
- 新增 `prototype/eval/extraction_batches.json`，把 extraction 核心样本目录显式收口到 `legacy_benchmark_core`
- 新增 `prototype/eval/extraction_issue_taxonomy.json`，把 issue_type 收口到 `benchmark_coverage / content_fidelity / outline_structure / parser_integrity / section_split_regression`
- 新增 `prototype/scripts/extraction_diagnose.py`，把单文档诊断、evidence pack、pattern candidate、diagnosis summary 收口到统一入口
- 新增 `prototype/eval/extraction_patterns.json`，沉淀 `heading_recovery / scope_and_namespace / table_and_clause_binding` 三类 pattern library
- `prototype/eval/quality_gate_profiles.json` 已补齐 `sor_baic_core`、`pds_de_oil_pump_v1_1`，并放宽 `q_jly_2024` 文件名匹配
- 已产出可复跑 batch 工件：`prototype/eval/extraction_batches/extraction_quality_loop_20260409_05`
- 已产出单线闭环 batch 工件：`prototype/eval/extraction_batches/extraction_quality_loop_20260411_singleline_audit`
  - 新增 `diagnosis_summary.json/.md`
  - 新增 `pattern_candidates.json`
  - 新增 `per_doc/*.diagnosis.json`
- 定向验证已通过：
  - `prototype.tests.test_extraction_diagnose`
  - `prototype.tests.test_extraction_batch_report`
  - `prototype.tests.test_parser_merge`
  - `prototype.tests.test_extractor_sections`
  - `prototype.tests.test_complex_table_mode`
- 新增预览 / benchmark 收口修复：
  - `prototype/app/services/extraction_preview.py`
    - `LOCAL_CLAUSE_RE` 放宽到支持顶层编号 clause
    - 本地 clause promotion 允许保留 `1..12 / 6.1 / 6.4 / 7.1 / 8.2.5.4 / 11.12` 等 anchor
    - `requirements_count` 改为基于 dedup 后 `complete_entries` 统计
    - 新增 fidelity section 计数规则：主文档 preview-only synthetic local split 不再计入 benchmark `sections_count`
  - `prototype/scripts/fidelity_benchmark.py`
    - `sections_count` 改为只统计 outline section 与 appendix/test-outline scoped synthetic section
    - required code / title 校验仍保留对完整 section 集合的检查
- `prototype/app/services/extractor.py` 已补 synthetic appendix scope 轮转逻辑，收敛 `SUB1` raw duplicate scoped code
- 新增回归覆盖：
  - `prototype.tests.test_unified_workspace.UnifiedWorkspaceTests.test_preview_promotes_local_reset_clause_heading`
  - `prototype.tests.test_unified_workspace.UnifiedWorkspaceTests.test_fidelity_section_count_skips_main_doc_synthetic_local_split`
- 油泵 PDS 当前已拉回绿色：
  - quality：`10/10`
  - fidelity：`22/22`
  - 当前关键指标：`sections_count=81`、`requirements_count=100`
- 已确认油泵 PDS 关键根因链：
  - `3.2.2` 下真实存在 `1..12` 与 `6.1/6.4/7.1/8.1.2/8.2.5.4/11.12` 等本地结构
  - 当前已修复“保留 local anchor 但避免 `sections_count` 因 preview-only synthetic split 膨胀”的 benchmark 口径
- 已澄清 `SUB1` 问题口径：
  - `SUB1` 是 synthetic appendix scope，本身被跨页复用并非一定错误
  - 当前真正暴露的是 raw outline 层 `SUB1 + code` 重复过多；下游 `section_instance_key` 只能部分兜底，structural gate 仍会按裸 `scoped_code` 报红

## 当前 blockers

- `legacy_benchmark_core` 当前整体为红：
  - structural gate 仍有 `3` 个 high severity issue
  - fidelity 仍由 `Q-JLY / SOR / damper` 拦截
- 当前主 blocker 已收口为三类：
  - `Q-JLY`
    - `requirements_count=451`，对基线 `398` 漂移 `53`
    - `section_consistency_issues=8`，高于阈值 `3`
  - `SOR-N82KS`
    - `requirements_count=172`，低于最小阈值 `180`
    - 缺 `F:1 / G:1`
    - `E:2` 标题仍漂移
  - 减振器 PDS
    - `sections_count` 已回到 band：`245` vs expected `234`
    - 仍卡在 `3.2.2` requirement text / ordered snippet / table row / forbidden cross section binding 漂移
- 当前已确认 scope 边界：
  - 当前 track 同时负责 `batch regression + document-level audit + pattern capture + benchmark-safe rule promotion`
  - 允许维护 `parser / extractor / extraction_preview / scripts / tests / eval / 当前 track skill / active-context`
  - 仍不做产品侧 audit job、API、UI 与主运行时接入

## 下一步

- 等当前 `extraction_batch_report` 收尾后刷新 `diagnosis_summary / per_doc diagnosis / pattern_candidates`，使单线 batch 工件与最新 benchmark 一致
- 下一轮优先级：
  - `Q-JLY`：继续压 requirement 膨胀与 consistency drift，重点看 local clause split + complex table rewrite 联动
  - `SOR-N82KS`：恢复 `F:1 / G:1` 与 `E:2` 标题
  - 减振器 PDS：专修 `3.2.2` complex table clause binding，不再回头搭框架
- 新文档默认先跑 `prototype/scripts/extraction_diagnose.py`，确认是真回归、profile 问题还是新文档模式，再决定是否进入核心 batch
- 修复后按固定顺序：聚焦测试 -> 单文档复验 -> `legacy_benchmark_core` -> pattern promotion 记录 -> control tower gate
- control tower 最新 checkpoint 已记录：油泵 benchmark 已回绿；新窗口直接从当前 active-context 与 `extraction_quality_loop_20260411_singleline_audit` 产物继续

## 活跃端口

- 控制塔页面：`/project-progress`
- 应用主端口：`8000`
