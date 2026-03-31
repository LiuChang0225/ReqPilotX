# Active Context

更新时间：2026-03-31 10:29 CST  
适用仓库：`/Users/liuchang/Documents/New project`

长期背景：[`/Users/liuchang/Documents/New project/docs/项目.md`](/Users/liuchang/Documents/New%20project/docs/项目.md)

## 当前目标

- 在 `main + 8000` 上保持“非 Azure 自动 AI 判定 Copilot”稳定，不再继续扩改本地主链
- 当前主线目标改为：为公司网络中的 Azure live 验证提供清晰交接
- extraction / table-assets 当前转入维护态，仅在出现明确回归时继续深挖
- Azure 路径的代码边界已经接好，下一步按 [`/Users/liuchang/Documents/New project/docs/Azure环境接手清单.md`](/Users/liuchang/Documents/New%20project/docs/Azure%E7%8E%AF%E5%A2%83%E6%8E%A5%E6%89%8B%E6%B8%85%E5%8D%95.md) 执行

## 当前样本

- benchmark：`/Users/liuchang/Documents/New project/prototype/storage/uploads/6fd15d3fb7104ea882d544d10f32372d_产品设计规格说明书PDS-DE前后油泵总成-V1.1-20251215.pdf`
- 回归样本：`/Users/liuchang/Documents/New project/prototype/storage/uploads/1e4079e33aef4432bb379d5d0ef95d50_产品设计规格说明书PDS-DE全主动减振器总成-V1.0-20251127.pdf`

## 最近已修

- `prototype/app/services/ai_copilot.py`
  - 新增统一 AI Copilot pipeline：assessment context、retriever/engine/reconciler、fallback、trace
  - 支持 `heuristic / llm_shadow / llm_primary` 三种模式切换
  - 新增 knowledge chunk payload 生成；当前默认落本地 payload，并为 Azure/Haystack 检索保留接入口
  - 新增 requirement screening：先把保留条目分成 `REQUIREMENT / INFO`
  - `INFO` 只落标签与 `analysis_basis`，不再进入 retrieval/decision
  - 无知识库时，`REQUIREMENT` 只做 `screening_only`，不再硬落一个伪 `NEED_CLARIFICATION`
  - 本地 heuristic retrieval 现已改成 `chunk + metadata boost`，优先 active 文档、table slice、法规类文档
  - 本地 rule reconciler 现已补上第一轮硬规则：
    - 冲突证据默认回退 `NEED_CLARIFICATION`
    - 无负向证据不得直接 `REJECT`
    - 法规类缺少 compliance signal 时不得正向判定
    - 强负向证据可覆盖错误的 `ACCEPT/DEVIATION_ACCEPT`
  - Azure OpenAI 现支持 `REQPILOT_AZURE_AUTH_MODE=api_key|rbac|auto`
  - Azure AI Search 现支持两条接入路径：
    - `azure_haystack`：继续给 API key 环境使用
    - `azure_sdk`：给 RBAC/AAD 环境使用
  - 新增 `AZURE_OPENAI_API_VERSION`、`REQPILOT_AZURE_ALLOW_INTERACTIVE_LOGIN` 配置位
  - 当前在未配置 Azure/LLM 时，判定链路仍是“本地 lexical retrieval + heuristic assessor”
- `prototype/app/db.py`
  - `assessments` 新增 `assessment_mode / normalized_requirement / risk_level / clarification_question`
  - `assessments` 新增 `analysis_label / analysis_basis`
  - 新增 `assessment_runs` 审计轨迹表
  - `knowledge_documents` 新增 `doc_type / doc_version / status / effective dates / retrieval sync` 元数据
  - `tests.test_unified_workspace` 的 SQLite tempdir `-wal/-shm` 清理抖动已通过连接收口 + test teardown 重试规避
- `prototype/app/main.py`
  - `run-ai-analysis` 已切到 AI Copilot pipeline，自动落库扩展 assessment 字段、evidence 与 assessment runs
  - `run-ai-analysis` 现在先跑全量 screening，再只对 `REQUIREMENT` 做 assessment
  - `run-ai-analysis` 现会返回 `ai_processed_requirements / ai_info_requirements / ai_screened_requirements`
  - 无 knowledge docs 时不再 422；会先完成 `INFO`/`REQUIREMENT` 标注
  - `INFO` 行已禁止进入人工 final decision/batch decision
  - knowledge 文档上传后会立即生成 retrieval payload，并回写 sync 状态
  - 新增 `POST /api/requirements/{requirement_id}/analysis-label`，支持人工把 `INFO` 覆盖回 `REQUIREMENT` 并重新分析
  - workspace/document summary 现已暴露 `configured_assessment_mode / configured_retrieval_backend / configured_azure_auth_mode`
- `prototype/app/static/app.js`
  - matrix 改成独立 `AI 结果` 列，当前只展示 `AI 标签/判定结果 + 依据`
  - `INFO` 行显示 `INFO + analysis_basis`
  - `screening_only` 的真实需求显示 `待分析`，不再误显示为空白 AI 结果
  - 去掉主视图里的 `置信度 / 风险 / 澄清问题 / 规范化需求`
  - `INFO` 行现在支持“改为需求”并重新进入 AI 分析
  - knowledge docs 上传表单现已暴露 `doc_type / doc_version / status / effective dates`
  - workspace 新增知识文档清单，直接展示 metadata 与 retrieval sync 状态
- `prototype/app/static/styles.css`
  - 同步新增紧凑型 `AI 结果` 卡片样式、knowledge docs 列表与行内操作样式
- `prototype/start.sh`
  - 启动时如果 PID 文件对应进程仍在但健康检查失败，会先清理旧进程再启动，避免旧 PID 误判
- `prototype/status.sh`
  - 新增 `PID_STATUS`，明确区分 alive / stale
- `prototype/stop.sh`
  - 停止监听进程后会等待端口真正释放，再结束脚本
- 文档体系
  - `/Users/liuchang/Documents/New project/docs/项目.md` 已重写为当前总账，新增文档地图、模块盘点、待开发/待完善/待测试清单
  - `/Users/liuchang/Documents/New project/docs/开发执行清单.md` 已新增可执行任务卡，按模块整理 owner、输入、输出、依赖与验收标准
  - `/Users/liuchang/Documents/New project/prototype/README.md` 的 roadmap 已对齐到当前主线
  - `/Users/liuchang/Documents/New project/prototype/docs/p0_p1_execution_backlog.md` 已明确标为历史执行清单
  - `/Users/liuchang/Documents/New project/docs/sor-ai-app-ai-prompts-rules-v1.md` 已补“历史模板”说明，避免误用为当前真相源
- `prototype/tests/test_ai_copilot.py`
  - 新增 `INFO` 预筛、AI pipeline、fallback、knowledge indexing 单测
- `prototype/tests/test_unified_workspace.py`
  - 新增 knowledge metadata 上传测试
  - 新增 `INFO -> REQUIREMENT` 人工覆盖回归
  - 当前 `tests.test_ai_copilot + tests.test_unified_workspace` 顺序跑通
- `prototype/app/services/table_assets.py`
  - 修掉单行条目里 `DV/PV` 等行内单元格误判成 `end_anchor`，避免 `T:4 / T:5 / U:37 / V:1` 这类截图把整页尾巴带进来
  - 增加“晚到的泛化起点锚点”保护，`项目` 这类 generic cell 不再把 `W 整车应用基本信息` 裁成空白
  - 收紧 source retry：两列表/小信息表不再从正确 candidate-window 回退成错误 clause-window
  - 收紧 numbered heading 的 anchor fragments，`46/47/94/95` 这类 heading 不再拆成宽泛中文片段，避免命中更早的相似行
  - heading 上下文现在保留 section code，避免 `46`、`94` 之类试验条目丢编号后模糊匹配
  - 新增“干净单行条目忽略紧贴下一编号锚点”的保护，避免 `U:96 防尘罩拔脱` 这类条目被下一行 `63 EMC` 压成超薄截图
  - `source snapshot` 签名版本已提升到 `source-crop-v57`，配合 preview build token 自动淘汰旧 PNG
- `prototype/app/static/app.js`
  - 修正 table view 默认变体判断：当后端明确给出 `export_variant=slice` 时，前端不再无条件强切到 `source`
- `prototype/app/static/extraction-review.js`
  - 同步修正 review 页 table variant 逻辑
- `prototype/app/services/preview_cache.py`
  - preview cache 当前版本提升到 `preview-cache-v28-numbered-heading-anchor-fragments`
  - cache 现在额外校验 `_preview_build_token`，source-crop 版本变化会把旧 preview 直接判 stale
- `prototype/tests/test_table_assets.py`
  - 新增回归覆盖：inline cell end-anchor、late generic start-anchor、compact info table retry、heading anchor context、plain-numbered heading fragments、`U:96/63` 单行误裁场景
  - 新增 PDF 脏提取修复回归：碎裂中英混排标签、碎裂小数、公差列表、错位中文顿号
- 本地回归门禁
  - `prototype/scripts/ai_smoke_suite.py` 已建立，当前 `6/6` 通过
  - `prototype/scripts/table_asset_regression_gate.py` 已建立，当前 `137` 条 `table_assets` 回归通过
  - 核心非 Azure 套件顺序跑通：
    - `tests.test_ai_copilot`
    - `tests.test_unified_workspace`
    - `tests.test_table_assets`
    - `tests.test_complex_table_mode`
    - `tests.test_extractor_sections`
    - `tests.test_parser_merge`
    - 合计 `270` 条通过
- 真文档回放：
  - damper 两个文档副本 `7d914049...` / `ec8c4168...` 都已重建到 `preview-cache-v28-numbered-heading-anchor-fragments`
  - 主文档关键截图当前已验证：
    - `W 整车应用基本信息` 不再空白
    - `U:46` 当前 source snapshot 高度约 `56px`
    - `U:94` 当前 source snapshot 高度约 `58px`
    - `U:57/58/59/60` 当前 source snapshot 已重新生成并拆分成 4 张不同图片，不再复用同一张旧缓存
    - `U:96 / REQ-0231` 当前 source snapshot 路径仍为 `src_61100411333c474a.png`，高度已从 `44px` 回升到 `83px`
    - 用户已确认 `U:96` 页面观感正常，问题收口
  - oil pump benchmark：
    - 此前出现的 PDS 第 30 页缺失已确认解决

## 当前 blockers

- 当前无已知 `table_assets / source snapshot` 主线 blocker
- 当前开发环境不在公司网络；Azure RBAC 代码路径已接好，但 `llm_shadow / llm_primary` 仍未做真实云端验证
- 当前剩余 blocker 主要是 Azure live 联通与云端评测，不再是本地非 Azure 能力缺口
- 当前交接文档已单独整理到 [`/Users/liuchang/Documents/New project/docs/Azure环境接手清单.md`](/Users/liuchang/Documents/New%20project/docs/Azure%E7%8E%AF%E5%A2%83%E6%8E%A5%E6%89%8B%E6%B8%85%E5%8D%95.md)

## 下一步

- 切到公司网络后，先按 [`/Users/liuchang/Documents/New project/docs/Azure环境接手清单.md`](/Users/liuchang/Documents/New%20project/docs/Azure%E7%8E%AF%E5%A2%83%E6%8E%A5%E6%89%8B%E6%B8%85%E5%8D%95.md) 跑 Step 0 到 Step 4
- 先验证 requirement screening 的真实 Azure OpenAI 调用，再做 Azure AI Search 的 `index upsert / hybrid retrieval`
- 用真实 knowledge docs 做一轮 benchmark，确认自动 decision、evidence trace、fallback 行为
- 保持 `INFO` 条目继续进入 Compliance Matrix，只作为标签行存在，不参与自动判定
- 继续补领域规则，但当前第一轮本地 hard-rule 已可支撑非 Azure 生产使用
- 把 `QA-01` 后半段收口到 Azure smoke 与真实评测集，不再重复补本地 smoke
- 若用户继续报具体截图编号，仍沿用当前 `v28` 链路复现并打印 `start_anchor / end_anchor / row_slice / crop plan`
- 日常看文档统一顺序：
  - `docs/项目.md`
  - `docs/开发执行清单.md`
  - `docs/Azure环境接手清单.md`
  - `docs/active-context.md`
  - `prototype/docs/extraction-active-context.md`
- 日常使用统一走：
  - `cd prototype && ./start.sh`
  - `cd prototype && ./status.sh`
  - `cd prototype && ./stop.sh`
  - 调试时用 `cd prototype && ./start-dev.sh`

## 活跃端口

- `8000`：当前 `main` 稳定 review/workspace 端口（`./start.sh` + `screen`），当前 PID `66840`，`./status.sh` 为 `HTTP: 200 OK`
