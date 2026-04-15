# Active Context

更新时间：2026-04-15 11:14 CST  
适用仓库：`/Users/liuchang/Documents/New project`

长期背景：[`/Users/liuchang/Documents/New project/docs/项目.md`](/Users/liuchang/Documents/New%20project/docs/项目.md)

## 当前目标

- 在 `extraction-quality-loop` 轨道内建立可复跑的批量拆解回归闭环：样本清单、问题分类法、统一报告、控制塔 gate 同步
- 用统一 batch 明确既有 benchmark 当前状态，重点盯住 `Q-JLY`、`SOR-N82KS`、减振器 PDS、油泵 PDS
- 当前支线已改为单线闭环：`新文档测试 -> 结构审计 -> 问题定位 -> 通用修规则 -> 回归验证 -> 模式沉淀`
- `audit` 不再是单独日常分支，而是 `extraction-quality-loop` 内置子流程；当前不做产品侧 audit job / API / UI 接入
- 当前额外聚焦油泵 / 减振器 appendix scope 收口：用统一 `MAIN / SUBn` 输出替换外露 `T/U/V/W/X/Y`，同时保留 `appendix_title` 并把 `section_tree` 父子链直接呈现在“章节总览”中

## 活跃分支 / 工作树

- 当前执行支线：`codex/extraction-quality-loop`
- 当前 worktree：`C:\rp-eq`
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

- 已将 `main@cfb9e18 Improve review UI: upload progress bar, chip filter, column sizing, PDF layout, SOR removal` 合入当前支线：
  - 当前支线已同步 mainline 的上传进度条、kind chip filter、PDF 面板高度收口与当前 SOR 移除能力
  - `prototype/app/db.py`、`prototype/app/main.py`、`prototype/app/static/app.js`、`prototype/app/static/index.html`、`prototype/app/static/styles.css` 已完成 merge
  - `workspace` 主页面资源版本已统一 bump 到 `20260415-review-merge1`，避免浏览器继续命中 merge 前缓存

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
- 减振器 PDS 本轮已落地局部修复：
  - 仅在 `extractor._split_entries_by_local_clause_headers()` 中为已建立 deep local clause 上下文的 DVP tail reset 行启用 `12、/13、/14、/15、` 切分
  - 不再放宽 `extraction_preview` 的全局 local clause regex，避免重现 `4::4 / 5::5 / 6::6` 类 formal heading 误提升
  - 当前真实 PDF 预览已确认恢复 `3.2.2::12 / ::13 / ::14 / ::15`
  - 定向新增回归已通过，`prototype/tests/test_extractor_sections.py` 当前全量 `44/44` 通过
- 油泵 PDS 本轮新增保守修复：
  - `1.3.1` 这类已经在 line-entry 阶段分开的深层子节，不再因 section-header 长标题 heuristic 被吞回父节
  - 修复方式不是放宽全局长标题规则，而是仅在 `current section == candidate parent` 的父子节上下文下恢复 `x.y.z` header，并从长句首段抽取短标题
  - 当前真实 PDF 预览已确认 page 13 恢复 `SUB2:1.3.1 电子电气部件`
- 附件命名本轮已收口：
  - 油泵连接器相关 test-outline 附件不再暴露 synthetic `W / X / Y`
  - 当前改为使用源文档真实附件标题：`整车用电器与线束匹配评估表`、`测试分组 / Test Groups（推荐）`、`用电器试验大纲`
  - `extraction_preview` 已同步支持从 `Appendix <真实标题>` anchor 解析 scope
- 本轮继续补齐残余 fallback 收口：
  - `extraction_preview._iter_test_outline_candidates()` 已移除 `("Y", "用电器试验大纲")` 旧回退，preview / extractor 只保留 `W/X/Y -> 真实标题` 的兼容归一化，不再向外输出单字母 scope
  - `extractor._extract_test_outline_rows()` 继续把 `测试分组 / Test Groups（推荐）` 视为 reference-only 引导表，不再把分组表的 `1~6` 截图误插入 `6` 和 `7` 之间
  - 针对以上路径新增回归并通过：真实标题 electric-outline marker、reference-only test-group skip
- 本轮 review 可视化已落地：
  - `app.js` 与 `extraction-review.js` 的“章节总览”改为直接消费 `preview.section_tree`
  - 前端现在显式展示树节点缩进、层级、`structural_parent_section_*` 父节点标签，而不是只给扁平表格
  - 已同步 bump 静态资源版本：`app.js?v=20260414-section-tree1`、`extraction-review.js?v=20260414-section-tree1`
- 本轮在线验证已完成：
  - `5555` 服务实际未挂，已重启到当前 worktree 代码
  - 强制 `refresh-preview` 后，油泵 workspace preview 已确认不再出现 `appendix_scope=Y` 或 `Appendix Y`
  - 定向回归：`tests/test_extractor_sections.py + tests/test_unified_workspace.py = 147 passed`
- 本轮继续完成 appendix/tree schema 收口：
  - 新增 `prototype/app/services/appendix_scopes.py` 作为共享 appendix scope helper，统一 `MAIN_SCOPE_ID / MAIN_SCOPE_TITLE`、真实 test-outline 标题、以及 `T/U/V/W/X/Y -> SUB*` 兼容归一化
  - `extraction_preview` 现已先构建 canonical scope catalog，再对外统一输出 `MAIN / SUB1 / SUB2 ...`，同时保留 `appendix_title`
  - `section_tree` 现已注入 `MAIN/SUB*` root 节点，并拆分 `structural_level` 与 `numbering_level`
  - `CX` complex-table helper 行已从最终 `section_tree` / benchmark chapter-tree 剔除，只保留 review/table 资产链路
  - section consistency 现已把 source-faithful 重号改标注为 `source_faithful_duplicate_code`，并新增 `missing_scope_root` 注记，不再把这类源文档事实误当修复目标
- 本轮前端 / 在线校验已确认：
  - `app.js` 与 `extraction-review.js` 已同步展示 canonical scope、`结构层级 / 编号层级`、以及 `ROOT` scope root 行；静态资源版本已 bump 到 `scope-tree2`
  - `5555` 服务因 `start.ps1` 默认非 `--reload`，本轮已显式重启到当前 worktree 后再强制 `refresh-preview`
  - live oil-pump preview 当前 scopes=`MAIN,SUB1..SUB7`、root_count=`8`、`CX=0`、`structural_level/numbering_level` 全量存在
  - live damper preview 当前 scopes=`MAIN,SUB1..SUB6`、root_count=`7`、`CX=0`、`structural_level/numbering_level` 全量存在
  - live 两份 benchmark preview 均已确认不再向外输出 `T/U/V/W/X/Y/CX`
- 本轮继续完成 appendix boundary / complex overlay 收口：
  - `extractor` 与 `extraction_preview` 现已统一输出 `appendix_scope_evidence` 与 `appendix_title_source`，当前 evidence 分层为 `explicit_marker / toc_derived / heading_derived / table_header_derived`
  - synthetic appendix scope 遇到新的附件标题证据时会主动切新 `SUBn`，不再只依赖 duplicate-code lookahead；油泵 `软件开发要求 -> FMEA与特殊特性` 与 `附件目录 -> 附件4--诊断、OTA相关标准接入认证设计规范` 已有回归锁定
  - electric test-outline 在缺少更强标题证据时，preview / extractor 当前统一回退 generic family title `试验大纲`，同时保留 evidence/source 以便后续补真标题
  - `rewrite_preview_sections_for_complex_tables()` 已改为保留 source-faithful section row，并只追加 `complex_*` overlay 元数据，不再把 `CX` review zone 当成 section rewrite
- 本轮新增验证结果：
  - `prototype/tests/test_extractor_sections.py -q`：`53 passed`
  - `prototype/tests/test_unified_workspace.py -q -k "canonical_section_tree or appendix or structural_parent or section_tree"`：`4 passed`
  - `prototype/tests/test_unified_workspace.py -q` 在当前 Windows 机器上仍可能被临时 SQLite 清理问题打断；这不是本轮 scope/tree 变更引入的新失败
- 本轮回归补充验证：
  - `prototype/tests/test_extractor_sections.py -q`：`53 passed`
  - `prototype/tests/test_unified_workspace.py -q`：`98 passed`
  - `prototype/tests/test_table_assets.py -q`：`137 passed`
- 本轮 mainline merge 后回归补充验证：
  - `node --check prototype/app/static/app.js`：通过
  - `node --check prototype/app/static/extraction-review.js`：通过
  - `prototype/tests/test_unified_workspace.py -q`：`98 passed`
  - `prototype/tests/test_extractor_sections.py -q`：`53 passed`
  - `prototype/tests/test_table_assets.py -q`：`137 passed`
- 本轮 live 排查与 UI 收口已完成：
  - 已确认用户先前看到的油泵 `SUB1` 旧树来自 `5555` 上的旧非 `--reload` 进程，而不是当前 worktree backend
  - 重启 `5555` 并对油泵文档强制 `refresh-preview` 后，live preview 已确认：`软件开发要求 -> SUB1`、`FMEA与特殊特性 -> SUB2`
  - `网络安全 / 数据安全技术要求矩阵` 与 `产品网络安全生命周期矩阵` 已确认只来自 `complex_table_mode.py` 的 review overlay title，不再作为普通 `sections` 章节行对外展示
  - `workspace` 与 `extraction-review` 的“章节总览”已同步改成更轻量的树视图：去掉 `编号层级 / 需求条数` 列，合并 node+title 展示，保留 scope / 父节点 / 页范围 / 结构层级
  - 静态资源版本已 bump 到 `section-overview3`，live `5555` 已确认返回新 HTML/JS 资源引用
- 本轮章节总览 UI 再次收口：
  - `workspace` 与 `extraction-review` 当前都已改成动态 `L0..Ln` 列式树路径展示，按真实 `section_tree` 深度生成层级列
  - 已移除“父节点 / 结构层级”列，章节父子关系改由横向路径直接表达，页范围与额外指标保留在尾部
  - 静态资源版本已 bump 到 `section-overview4`，live `5555` HTML 已确认不再输出旧表头
- 本轮章节总览 UI 继续优化：
  - `workspace` 的“开发诊断”栅格已改成偏右布局，压缩“解析诊断摘要”横向占比，把更多宽度留给“章节总览”
  - 章节总览区域已提升到近一屏高的滚动面板，适配 `1920x1080` 级别人工复核时的整屏阅览
  - 节点卡片当前只对 `ROOT` 保留附加标题；普通章节不再显示重复副标题，避免 `1 概述 / 概述`、`1.1 长句 / 去编号长句` 这类重复占位
  - 静态资源版本已 bump 到 `section-overview5`，live `5555` 已确认返回新版 HTML/JS/CSS 引用
- 本轮章节总览 UI 最终收口到 `section-overview6`：
  - `workspace` 的“解析诊断摘要 : 章节总览”横向分配已固定为 `2 : 5`
  - 旧的 `review-mini-table max-height: 240px` 限高已移除，章节总览当前按近整屏高度滚动展示，不再只露两行
  - `附录域 / L0 / L1` 当前已做 sticky 列，深层横向滚动时主干结构不会丢
  - `extraction-review` 也已同步 sticky scope/L0/L1 逻辑，便于两页对齐复核
- 本轮章节总览继续收口到 `section-overview7`：
  - `附录域` 继续保留在第一列，但章节路径现在已显式剔除 appendix scope root；`L0` 从第一个真实章节开始，不再浪费一列重复显示 `ROOT` / scope root
  - `workspace` 与 `extraction-review` 当前都已把 overview 容器改成 1080 口径下的固定高度滚动区；浏览器实测 `workspace` 章节总览当前 `clientHeight≈709px`、`scrollHeight≈22094px`、`overflow-y=auto`
  - 浏览器实测当前 `sampleLabels` 已不再包含 `ROOT`，live 页面资产版本已切到 `section-overview7`
- 本轮 workspace 项目下拉补齐了失效选择回退：
  - live `/api/projects` 已确认仍正常返回项目，当前问题不是后端缺数据
  - 根因是前端会恢复本地记忆的 `projectId`，但旧值失效时没有回退到当前有效项目，导致“当前项目”选择器停在无效状态
  - `app.js` 当前已改为：若恢复出的 `projectId` 不在最新项目列表中，则自动清空并回退到首个可用项目；若恢复出的 `projectId` 仍有效，也会在 `loadProjects()` 后继续触发 workspace hydration，不再只停留在“选择器看起来有值”
  - 后续彻查又确认还有第二个真实根因：`app.js` 的 `renderStructuredTable()` 被混入了一行错误的章节总览 `<td class="section-overview-scope-cell ...">` 模板，浏览器在解析到该处时会直接抛 `Unexpected token 'class'`，导致 `init()` / `loadProjects()` 根本不执行
  - 当前已移除这行脏模板并补回缺失的 `.join("")`；`workspace` 静态资源版本已继续 bump 到 `app.js?v=20260414-project-select-fix3`
  - 已用本机 Edge + Playwright 直接打开 live `/workspace` 验证：`#project-select` 当前 `optionCount=2`，默认已选中 `Damper Branch Debug / local`，且 `PAGE_ERRORS=[]`
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
    - `3.2.2` 下 `12/13/14/15` 本地条款拆分已恢复；剩余 blocker 继续收口到 requirement text / ordered snippet / table row / forbidden cross section binding 漂移
- appendix/tree 这条线上当前还保留一个 source-faithful 注记：
  - live oil-pump preview 当前 `section_consistency_missing_scope_root_count=1`
  - 当前按用户确认范围仅做标注，不改源文档内容或强行重写章节事实
- 本轮 appendix boundary / complex overlay 已进入新阶段：
  - 电器 test-outline 在缺少更强标题证据时当前统一回退 `Sub7 / 试验大纲`；若后续继续提可读性，应优先补邻近标题/目录恢复，而不是回退到历史 `Y`
  - `appendix_scope_evidence / appendix_title_source` 已写入 extractor / preview 的 row 与 section 数据；benchmark/report/UI 目前还未系统消费这些置信度字段
  - complex-table backend 已保留 source-faithful section row 并打 overlay 元数据；后续若要在 UI 上高亮 complex family/group，应消费 overlay 字段，而不是恢复 `CX` section rewrite
- 当前执行层仍需注意一个环境风险：
  - 本地基线已提交：`4c43104 Canonicalize appendix scopes and tree roots baseline`
  - 这台 Windows 机器的 GitHub push 历史上受 TLS/EOF 握手波动影响；如需再次 push，优先沿用 `schannel + HTTP/1.1 + tls1.2` 组合
- 当前已确认 scope 边界：
  - 当前 track 同时负责 `batch regression + document-level audit + pattern capture + benchmark-safe rule promotion`
  - 允许维护 `parser / extractor / extraction_preview / scripts / tests / eval / 当前 track skill / active-context`
  - 仍不做产品侧 audit job、API、UI 与主运行时接入

## 下一步

- 等当前 `extraction_batch_report` 收尾后刷新 `diagnosis_summary / per_doc diagnosis / pattern_candidates`，使单线 batch 工件与最新 benchmark 一致
- 下一轮优先级：
  - `Q-JLY`：继续压 requirement 膨胀与 consistency drift，重点看 local clause split + complex table rewrite 联动
  - `SOR-N82KS`：恢复 `F:1 / G:1` 与 `E:2` 标题
  - 减振器 PDS：在 `12/13/14/15` 拆分已恢复的基线上，继续专修 `3.2.2` complex table clause binding，不再回头放宽全局 regex
- 油泵这条线上当前只剩浏览器侧人工复核：确认用户端 hard refresh 后，“章节总览”能直接看到父子链，且连接器附件区域不再出现 `Y`
- 章节总览这条线上当前只剩浏览器侧人工复核：确认 hard refresh 后，新的 `L0..Ln` 树路径列、`2:5` 摘要/总览比例、整屏高度总览区、sticky `附录域/L0/L1`，以及 `SUB1/SUB2` 边界和 complex overlay 表现都与 live preview 一致
- workspace 下拉这条线上当前只剩浏览器侧人工复核：确认浏览器刷新 `/workspace` 后，已拿到 `project-select-fix3` 并能直接看到 `Damper Branch Debug / local`
- appendix/tree 这条线上下一步若继续收口，优先做三件事：
  - 利用现有 `appendix_scope_evidence / appendix_title_source` 给 batch report / review UI 补可见置信度，而不是再改 scope ID 口径
  - 继续缩小 legacy alias 使用面；当前对外输出与大部分测试夹具已切到 `MAIN/SUB*`，只保留 `appendix_scopes.py` 和 alias-normalization 回归中的内部兼容映射
  - 对 generic `试验大纲` fallback，优先补 source-title 恢复规则；只有标题证据不足时才保留 generic family title
- complex-table 这条线当前 backend 已改成 source-faithful chapter row + overlay 双轨；后续若继续推进，重点在前端消费 overlay，而不是恢复 `CX` section rewrite
- 新文档默认先跑 `prototype/scripts/extraction_diagnose.py`，确认是真回归、profile 问题还是新文档模式，再决定是否进入核心 batch
- 修复后按固定顺序：聚焦测试 -> 单文档复验 -> `legacy_benchmark_core` -> pattern promotion 记录 -> control tower gate
- control tower 最新 checkpoint 已记录：油泵 benchmark 已回绿；新窗口直接从当前 active-context 与 `extraction_quality_loop_20260411_singleline_audit` 产物继续

## 活跃端口

- 控制塔页面：`/project-progress`
- 应用主端口：`8000`
- 当前分支人工复核端口：`5555`
