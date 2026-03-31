# Extraction Active Context

更新时间：2026-03-31 10:29 CST  
分支：`main`

## 分支目的

- 用 oil pump PDS 维持 benchmark 基线
- 用 减振器 PDS 回归试验大纲截图精度
- 章节顺序已经稳定，当前 extraction/table-assets 转入维护态
- 当前以 `8000` 上的真实 review/workspace 结果为准
- `U:96 / REQ-0231` 与油泵 PDS 第 30 页缺失都已收口
- 当前新增主线：Azure 对齐的自动 AI 判定 Copilot 已在 `main` 接入 heuristic/local 版本，并补上独立 `INFO screening`
- 当前 Azure 接入已补成 `API key + RBAC` 双模式，但 live 验证仍受公司网络限制
- 当前本地非 Azure 链路已收口到可用状态：AI smoke `6/6`、table gate `137`、核心回归 `270` 条顺序通过
- 当前接手 Azure 联调时，统一先看 [`/Users/liuchang/Documents/New project/docs/Azure环境接手清单.md`](/Users/liuchang/Documents/New%20project/docs/Azure%E7%8E%AF%E5%A2%83%E6%8E%A5%E6%89%8B%E6%B8%85%E5%8D%95.md)

## 当前样本

- benchmark：`/Users/liuchang/Documents/New project/prototype/storage/uploads/6fd15d3fb7104ea882d544d10f32372d_产品设计规格说明书PDS-DE前后油泵总成-V1.1-20251215.pdf`
- 回归：`/Users/liuchang/Documents/New project/prototype/storage/uploads/1e4079e33aef4432bb379d5d0ef95d50_产品设计规格说明书PDS-DE全主动减振器总成-V1.0-20251127.pdf`

## 最近已修

- `prototype/app/services/ai_copilot.py`
  - 新增 assessment context、retriever/engine/reconciler、trace 统一 pipeline
  - 支持 `heuristic / llm_shadow / llm_primary`
  - knowledge 文档上传后会生成 local retrieval payload，并为 Azure/Haystack 接口保留入口
  - 新增 requirement screening，先把保留条目标记成 `REQUIREMENT / INFO`
  - `INFO` 只落 `analysis_label / analysis_basis`
  - 无 knowledge docs 时，真实需求只落 `screening_only`，等待后续 RAG/decision
  - 本地 heuristic retrieval 现已升级为 `chunk + metadata boost`
  - 本地 rule reconciler 已补第一轮 hard rules：冲突证据回退、负向证据保护、法规 compliance signal 收口
  - Azure OpenAI 现支持 `api_key / rbac / auto`
  - Azure AI Search 现支持：
    - API key 环境走 `azure_haystack`
    - RBAC 环境走 `azure_sdk`
  - 当前没有真实 Azure/LLM 配置时，仍走本地 lexical retrieval + heuristic assessor
- `prototype/app/db.py`
  - `assessments` 新增 `assessment_mode / normalized_requirement / risk_level / clarification_question`
  - `assessments` 新增 `analysis_label / analysis_basis`
  - 新增 `assessment_runs`
  - `knowledge_documents` 新增 metadata 与 retrieval sync 字段
- `prototype/app/main.py`
  - `run-ai-analysis` 已改走 AI Copilot pipeline
  - 自动落库扩展 assessment 字段、evidence 与 assessment runs
  - `run-ai-analysis` 现会先跑全量 screening，再只分析 `REQUIREMENT`
  - 无 knowledge docs 时不再 422，可先完成 `INFO`/`REQUIREMENT` 标注
  - `INFO` 行不再进入人工 final decision / batch decision
  - 新增 `INFO -> REQUIREMENT` 的人工覆盖 API，可直接重新进入分析
  - workspace 现可看到当前 `configured_azure_auth_mode`
- `prototype/app/static/app.js`
  - matrix 已改成独立 `AI 结果` 列
  - `INFO` 行显示 `INFO + analysis_basis`
  - `screening_only` 的真实需求显示 `待分析`
  - `置信度 / 风险 / 澄清问题 / 规范化需求` 已从主视图移除
  - `INFO` 行现在支持“改为需求”
  - knowledge upload UI 现已支持 metadata 输入
  - workspace 现已显示知识文档清单
- `prototype/start.sh` / `status.sh` / `stop.sh`
  - 本地 `start -> status -> stop` 已完成 smoke，旧 PID 误判口径收了一轮
- 文档体系
  - `/Users/liuchang/Documents/New project/docs/项目.md` 已更新为当前总账，包含当前主线、模块盘点、待开发/待测试清单
  - `/Users/liuchang/Documents/New project/docs/开发执行清单.md` 已新增可执行任务卡，供 Azure/AI 主线按模块排期
  - `/Users/liuchang/Documents/New project/prototype/README.md` roadmap 已同步到当前 AI 主线
  - `p0_p1_execution_backlog.md` 已标记为历史执行清单
- `prototype/tests/test_ai_copilot.py`
  - 新增 `INFO` 预筛、AI pipeline / fallback / indexing 单测
- `prototype/tests/test_unified_workspace.py`
  - 新增 AI assessment integration 覆盖
  - 新增 metadata 上传与人工覆盖 `INFO -> REQUIREMENT` 覆盖
  - SQLite tempdir `-wal/-shm` 清理抖动已通过 teardown 重试收口
- `prototype/app/services/table_assets.py`
  - 单行条目中，`DV/PV` / supplier / sample 这类行内单元格不再被误当作 `end_anchor`
  - generic start-anchor（如 `项目`）不再把 `W` 类小信息表裁成空白
  - compact labeled info table 不再从 candidate-window 误退回 clause-window
  - heading anchor context 现在保留编号；plain-numbered heading 不再被拆成宽泛中文 fragment
  - 新增“section_end_anchor 仅在脏单行场景继续参与收紧；干净单行且锚点紧贴下一编号行时忽略该锚点”的保护，修复 `U:96`
  - `source snapshot` 签名版本已提升到 `source-crop-v57`，用于强制失效旧截图缓存
- `prototype/app/services/preview_cache.py`
  - preview cache 当前版本为 `preview-cache-v28-numbered-heading-anchor-fragments`
  - preview cache 现在同时比较 `_preview_build_token`，crop 签名变化会自动判 stale
- `prototype/app/static/app.js`
  - 默认 table variant 现在尊重后端 `export_variant`
- `prototype/app/static/extraction-review.js`
  - review 页同步尊重后端 `export_variant`
- `prototype/tests/test_table_assets.py`
  - 新增 `W`、`U:46`、`U:94` 以及 `U:96/63` 这类问题对应的回归断言
  - 新增 PDF 脏提取修复回归：碎裂双语标签、碎裂小数、公差列表、错位中文顿号
- 回归门禁
  - `prototype/scripts/ai_smoke_suite.py` 当前 `6/6`
  - `prototype/scripts/table_asset_regression_gate.py` 当前 `137` 条通过
  - 核心非 Azure 套件顺序通过 `270` 条

## 当前样本状态

- oil pump：
  - benchmark 继续作为章节 / export 基线
  - 此前出现的 PDS 第 30 页缺失已确认解决
- damper：
  - 两个文档副本 `7d914049...` / `ec8c4168...` 都已重建到 `preview-cache-v28-numbered-heading-anchor-fragments`
  - `W 整车应用基本信息` 当前 source snapshot 不再空白
  - `U:46` 当前 source snapshot 约 `56px` 高
  - `U:94` 当前 source snapshot 约 `58px` 高
  - `U:57/58/59/60` 已确认根因是“新 crop plan 正确，但旧 PNG 在未换签名时被复用”；当前 4 张 source snapshot 已重建为不同文件
  - `U:96 / REQ-0231` 已确认根因是“下一编号行 `63 EMC` 被当作当前单行条目的结束锚点”；真实刷新后同一路径 `src_61100411333c474a.png` 高度已从 `44px` 回升到 `83px`
  - 用户已确认 `U:96` 页面效果正常
  - 这轮已确认顺序问题不再是主线

## 当前已知问题

- 当前无已知截图主线 blocker；若再报新编号，再按 anchor/crop plan 链路复现
- 当前开发环境不在公司网络，Azure RBAC 代码路径虽然已接好，但 `llm_shadow / llm_primary` 还未做真实云端验证
- 试验大纲/附件起点的人机确认锚点仍是第二期，不在本轮代码内
- Azure 侧下一步执行顺序、验证命令、通过标准，已单独整理到 `docs/Azure环境接手清单.md`

## 下一步

- 切到公司网络后，先按 `docs/Azure环境接手清单.md` 跑 screening-only smoke
- 再验证 Azure AI Search 的 `index upsert / hybrid retrieval`
- 用真实 knowledge docs 评估自动 decision 与 evidence trace
- 继续补领域规则，但当前本地第一轮 hard-rule 已足够支撑非 Azure 路径
- 把评测重点转到 Azure smoke 与真实 RAG benchmark，不再重复补本地 smoke
- 文档接续默认顺序：
  - `docs/项目.md`
  - `docs/开发执行清单.md`
  - `docs/Azure环境接手清单.md`
  - `docs/active-context.md`
  - `prototype/docs/extraction-active-context.md`
- 保持 `INFO` 条目继续进入 Compliance Matrix，只作为标签行存在
- 若还有残余问题，先打印：
  - `before_context / after_context`
  - `start_anchor / end_anchor`
  - `row_slice / crop plan`
- 版本/缓存策略默认继续走：
  - 每文档单套 live preview assets
  - 算法版本变化通过 build token 自动失效，不保留大量历史 preview PNG
- 若 `8000` 不可达，优先执行：
  - `cd prototype && ./status.sh`
  - `cd prototype && ./start.sh`
