# Azure 环境接手清单

更新时间：2026-04-01 13:42 CST  
适用仓库：`/Users/liuchang/Documents/New project`

这份文档给“正式放行后切到公司网络的下一个 agent”使用。  
目的只有一个：在 Azure cloud 执行获批后，不重做本地工作、不重新摸索上下文，沿用已经验证通过的 Azure 路径恢复 App Service / Azure 联调，而不是重复踩坑。

长期背景：[`/Users/liuchang/Documents/New project/docs/项目.md`](/Users/liuchang/Documents/New%20project/docs/%E9%A1%B9%E7%9B%AE.md)  
执行总表：[`/Users/liuchang/Documents/New project/docs/开发执行清单.md`](/Users/liuchang/Documents/New%20project/docs/%E5%BC%80%E5%8F%91%E6%89%A7%E8%A1%8C%E6%B8%85%E5%8D%95.md)

## 0. 当前暂停状态

- 2026-04-01 起，Azure cloud / App Service 落地任务按当前阶段决策暂停，原因不是代码回退，而是尚未进入正式放行与权限申请窗口
- 正式放行前，不再继续 `azd provision --preview`、`azd up`、资源组创建、托管身份角色分配或新的云端部署尝试
- 需要保留的恢复点已经具备：`azure.yaml + infra/`、本机 `azd`、本地环境 `reqpilotx-dev`、已恢复的 Azure OpenAI/Search 基线、以及 management-plane RBAC 诊断结论
- 当前近端主线改为本地服务器运维：维护 `8000` 服务、`/api/health` / `/api/readiness`、Azure smoke/benchmark、smoke DB 隔离和人工抽查

## 1. 当前基线

- 当前 `main` 上，本地非 Azure 路径已收口到可用状态
- 仓库现已新增 Azure App Service 落地脚手架：根目录 `azure.yaml`，以及 `infra/main.bicep`、`infra/resources.bicep`、`infra/main.parameters.json`
- 已完成：
  - `INFO screening -> REQUIREMENT assessment -> trace`
  - `INFO` 条目保留在 Compliance Matrix，但不参与后续分析
  - 本地 retrieval 已升级为 `chunk + metadata boost`
  - 第一轮 hard-rule 已落地
  - knowledge docs metadata 上传、人工 `INFO -> REQUIREMENT` 覆盖、脚本健康检查已完成
- 本地验证基线：
  - `scripts/ai_smoke_suite.py`：`6/6`
  - `scripts/table_asset_regression_gate.py`：`137/137`
  - 核心非 Azure 回归：`270` 条顺序通过
- 当前 Azure live 基线已完成：
  - `scripts/azure_screening_smoke.py` 已通过，screening-only 路径为绿色
  - `scripts/azure_rag_smoke.py` 已通过，`index upsert -> retrieval + llm_shadow` 路径为绿色
  - `scripts/azure_rag_smoke.py` 现已参数化，支持自定义 `--sor-file`、重复 `--knowledge-file / --reference-file`、可关闭内置 table reference、可自定义 `--project-name`
  - `scripts/azure_rag_smoke.py` 现已新增 `--max-requirements`，可抽样保留少量真实 requirement 做最小可信 benchmark
  - 参数化 benchmark probe 已通过，说明脚本入口本身可直接承接真实样本
  - 外部真实样本 `sample10` 已通过：`532 -> 10` 条 requirement 抽样、`gpt-5.4 + azure_sdk + llm_shadow` 绿色通过，结果保存在 `prototype/eval/azure_smoke/rag_real_benchmark_sample10_gpt54`
  - 最新审计结果保存在：
    - `prototype/eval/azure_smoke/latest`
    - `prototype/eval/azure_smoke/rag_latest`
- 当前剩余工作不再是联通性 blocker，而是：
  - 用真实 knowledge docs / benchmark 文档扩大覆盖
  - 在人工抽查稳定后再评估是否切到 `llm_primary`
  - 如有需要，再单独收口 Windows smoke DB 清理 warning
- 当前工作区里仍没有内置真实 benchmark PDF / knowledge docs；当前最小可信 benchmark 依赖外部本机路径，跨机器复跑前需把文件放入工作区或共享可访问路径

## 1.2 Azure Web App 落地脚手架（新增）

- 当前云端首选宿主是 Azure App Service Linux Web App，不是 Azure Functions / Static Web App
- App Service 脚手架默认启用：system-assigned managed identity、Application Insights、Log Analytics、`/api/health` health check、单 worker `gunicorn`
- 运行时持久化目录通过 `REQPILOT_STORAGE_DIR=/home/site/data/reqpilotx/storage` 注入，避免继续依赖代码目录下的本地 `storage/`
- 应用内现在接受 `REQPILOT_AZURE_AUTH_MODE=managed_identity`，并自动映射到现有 `rbac` 流程，便于云端配置统一表达

推荐部署顺序：

```bash
cd '/Users/liuchang/Documents/New project'
azd env set AZURE_LOCATION eastasia
azd env set AZURE_OPENAI_ENDPOINT 'https://<your-openai>.openai.azure.com/'
azd env set AZURE_OPENAI_CHAT_DEPLOYMENT '<chat-deployment>'
azd env set AZURE_OPENAI_EMBEDDING_DEPLOYMENT '<embedding-deployment>'
azd env set AZURE_AI_SEARCH_ENDPOINT 'https://<your-search>.search.windows.net'
azd env set AZURE_AI_SEARCH_INDEX_NAME 'reqpilot-knowledge'
azd env set AZURE_AI_SEARCH_SEMANTIC_CONFIGURATION '<semantic-config>'
azd provision --preview
azd up
```

部署后立刻检查两件事：

- App Service 的 health check 是否命中 `/api/health`
- Web App 系统分配托管身份是否已授予现有 Azure OpenAI / Azure AI Search 所需角色

当前建议的角色最小集合：

- Azure OpenAI：`Cognitive Services OpenAI User`
- Azure AI Search：`Search Service Contributor`
- Azure AI Search：`Search Index Data Contributor`

当前仍保留一个硬约束：在 PostgreSQL 和外部任务队列落地前，云端实例数保持 `1`，Gunicorn workers 保持 `1`

最新执行结论：

- 本机已完成 `azd` 安装，并已创建本地环境 `reqpilotx-dev`
- 已按当前恢复的 Azure 基线注入：subscription `0adac5c3-3e8b-4670-9c15-cc95ea432b0f`、location `eastasia`、Azure OpenAI `oaicspq001reqpilotbdpsgcq`、Azure AI Search `cscspq001reqpilotbdpsgcq`、index `reqpilot-knowledge`
- 已确认目标资源确实位于 `Schaeffler Pre-Merger` tenant；当前 Azure token 解析到的 Entra 对象是 `chang.4.liu@vitesco.com`
- 该对象通过组 `RL_cspq001_reqpilotbdpsgcq_AI_DEVELOPER_AAD` 拥有部分组继承权限，因此当前 Azure smoke / Azure OpenAI / Azure AI Search 调用路径是可用的
- 当前已确认的组继承权限包括：资源组 `Reader`、Azure OpenAI `Cognitive Services OpenAI User` + custom OpenAI dev role、Azure AI Search custom Search dev role、Log Analytics Reader、Storage Blob Data Contributor
- `azd provision --preview` 已真实执行，但该对象仍缺少 `Microsoft.Resources/subscriptions/resourcegroups/write`，因此卡在资源组 `rg-reqpilotx-dev` 创建前
- 进一步对现有资源组 `ReG_cspq001reqpilotbdpsgcq` 执行 `az deployment group what-if` 时，又命中 `Microsoft.Resources/deployments/whatIf/action` 权限不足；这说明当前阻塞点是 management-plane RBAC，不是 `infra/` 模板语法或参数错误
- 当前阶段已明确暂停 `AZD-01`；恢复条件是正式放行 + 可申请/授予 management-plane 权限，恢复时先重跑 `azd provision --preview` 再继续

## 1.1 本轮 live 验证里已确认并修掉的问题

- `DefaultAzureCredential` 在本机 AAD 场景下可能优先拿到错误租户的 `VisualStudioCodeCredential / SharedTokenCacheCredential`；当前代码已显式排除这两条链路，改由 `AzureCliCredential` 命中正确租户
- `POST /api/projects/{project_id}/knowledge-documents` 曾一度把路由装饰器挂到错误函数，导致 multipart 上传 `422`；现已修正
- Azure Search document key 不能使用带 `:` 的 chunk id；当前 knowledge chunk key 已统一切成 safe id
- Azure Search reranker/search score 与本地 validator 口径不一致；当前已统一归一化到 `0..1`
- `llm_shadow` 引用 chunk 里的子句时，原先 exact excerpt 校验过严；当前已改成“同一 `source_doc_id + source_location` 下允许 excerpt 子串匹配”

## 2. 接手前先看

按这个顺序看，不要先翻旧 PRD：

1. [`/Users/liuchang/Documents/New project/docs/项目.md`](/Users/liuchang/Documents/New%20project/docs/%E9%A1%B9%E7%9B%AE.md)
2. [`/Users/liuchang/Documents/New project/docs/开发执行清单.md`](/Users/liuchang/Documents/New%20project/docs/%E5%BC%80%E5%8F%91%E6%89%A7%E8%A1%8C%E6%B8%85%E5%8D%95.md)
3. [`/Users/liuchang/Documents/New project/docs/active-context.md`](/Users/liuchang/Documents/New%20project/docs/active-context.md)
4. [`/Users/liuchang/Documents/New project/prototype/eval/progress/track_registry.json`](/Users/liuchang/Documents/New%20project/prototype/eval/progress/track_registry.json)
5. 当前 track 对应的 `prototype/docs/*-active-context.md`
6. 本文

## 3. 本轮不要重开的范围

- 不要重新改 `table_assets / preview / extraction` 主线，除非出现明确回归
- 不要为了 Azure 联通去回退已经通过的本地 heuristic 链路
- 不要一上来就切 `llm_primary`
- 不要把 `INFO` 当作一种 decision；它只是 screening label

## 4. 公司环境前置条件

- 已接入公司网络，能访问 Azure 资源
- 当前账号对 Azure OpenAI 和 Azure AI Search 至少有可调用权限
- 最好已有 `az login` 登录态
- 已知以下资源名或对应 endpoint：
  - Azure OpenAI endpoint
  - chat deployment
  - embedding deployment
  - Azure AI Search endpoint
  - Azure AI Search index name
  - semantic configuration name

RBAC 为主，API key 仅作为临时排障 fallback。

## 5. 必填环境变量

推荐先走 RBAC：

```bash
export REQPILOT_AZURE_AUTH_MODE=rbac
export REQPILOT_AZURE_ALLOW_INTERACTIVE_LOGIN=1

export REQPILOT_AI_ASSESSMENT_MODE=llm_shadow
export REQPILOT_RETRIEVAL_BACKEND=azure_sdk
export REQPILOT_AI_TOP_K=5
export REQPILOT_AI_TIMEOUT_SECONDS=45

export AZURE_OPENAI_ENDPOINT='https://<your-openai>.openai.azure.com/'
export AZURE_OPENAI_API_VERSION='2024-10-21'
export AZURE_OPENAI_CHAT_DEPLOYMENT='<chat-deployment>'
export AZURE_OPENAI_EMBEDDING_DEPLOYMENT='<embedding-deployment>'
export AZURE_OPENAI_EMBEDDING_DIMENSIONS='1536'

export AZURE_AI_SEARCH_ENDPOINT='https://<your-search>.search.windows.net'
export AZURE_AI_SEARCH_INDEX_NAME='reqpilot-knowledge'
export AZURE_AI_SEARCH_SEMANTIC_CONFIGURATION='<semantic-config>'
```

说明：

- `REQPILOT_AI_ASSESSMENT_MODE`
  - 先用 `llm_shadow`
  - 不要直接切 `llm_primary`
- `REQPILOT_RETRIEVAL_BACKEND`
  - RBAC 环境优先 `azure_sdk`
  - API key 环境才考虑 `azure_haystack`
- `REQPILOT_AZURE_ALLOW_INTERACTIVE_LOGIN=1`
  - 给 `DefaultAzureCredential` 的浏览器登录 fallback 用
  - 如果公司环境已通过 `az login`，也可以保留为 `0`
- RBAC 模式下不要再额外依赖 `AZURE_OPENAI_API_KEY`、`AZURE_AI_SEARCH_API_KEY`

## 6. 推荐执行顺序

### Step 0. 先确认本地基线没丢

```bash
cd '/Users/liuchang/Documents/New project/prototype'
PYTHONPATH=. .venv/bin/python scripts/ai_smoke_suite.py
PYTHONPATH=. .venv/bin/python scripts/table_asset_regression_gate.py
PYTHONPATH=. .venv/bin/python -m unittest -f \
  tests.test_ai_copilot \
  tests.test_unified_workspace \
  tests.test_table_assets \
  tests.test_complex_table_mode \
  tests.test_extractor_sections \
  tests.test_parser_merge
```

通过标准：

- `ai_smoke_suite.py` 仍是 `6/6`
- `table_asset_regression_gate.py` 仍是 `137/137`
- 核心回归仍通过
- 当前仓库已通过 `prototype/tests/__init__.py` 固定本地 `tests` 包解析；若仍遇到导入异常，先确认命令是在 `prototype/` 根目录执行

如果这一步失败，先不要碰 Azure，先把本地回归恢复。

### Step 1. 先做 Azure 身份确认

推荐先确认 CLI 登录态：

```bash
az login
az account show
```

如果公司环境需要指定订阅，再执行：

```bash
az account set --subscription '<subscription-id-or-name>'
```

通过标准：

- `az account show` 能正常返回账号和订阅
- 后续服务启动时不再出现明显的 AAD 凭据缺失错误

### Step 2. 先只验证 screening，不要混入 retrieval

目标是把 `AI-01` 单独跑通。  
做法：选一个已经锁定 requirements、但暂时没有 knowledge docs 的文档，直接点击 `运行 AI 分析`。

优先直接复跑脚本：

```bash
cd '/Users/liuchang/Documents/New project/prototype'
PYTHONPATH=. .venv/bin/python scripts/azure_screening_smoke.py
```

启动方式：

```bash
cd '/Users/liuchang/Documents/New project/prototype'
./stop.sh 8000 || true
./start.sh 8000
./status.sh 8000
```

UI 验证点：

- 进入 [http://127.0.0.1:8000/workspace](http://127.0.0.1:8000/workspace)
- 对已锁定 requirement set 的文档点击 `运行 AI 分析`
- `INFO` 行显示 `INFO + analysis_basis`
- 非 `INFO` 行显示 `待分析`
- 此时没有 knowledge docs，也不应报 422

数据库验证：

```bash
cd '/Users/liuchang/Documents/New project/prototype'
sqlite3 storage/app.db "
SELECT mode, retrieval_backend, model_name, validation_status, fallback_reason, created_at
FROM assessment_runs
ORDER BY created_at DESC
LIMIT 20;
"
```

通过标准：

- 能看到 `screening_llm_shadow` 记录
- `fallback_reason` 为空或仅有可接受的 provider 注记
- 没有大面积退回纯 heuristic screening
- 当前基线：该脚本最近一次结果已是 `overall_pass: True`

### Step 3. 再验证 Azure AI Search index upsert

目标是跑通 `AI-02`。  
这一步开始上传 knowledge docs，并且至少准备：

- 1 份段落型参考文档
- 1 份表格型参考文档

上传后检查：

- workspace 中知识文档清单出现 metadata
- `retrieval_sync_status` 有值
- 本地 payload 已写入 `prototype/storage/knowledge_index`

数据库检查：

```bash
cd '/Users/liuchang/Documents/New project/prototype'
sqlite3 storage/app.db "
SELECT file_name, doc_type, doc_version, status, retrieval_sync_status, retrieval_synced_at, retrieval_sync_error
FROM knowledge_documents
ORDER BY created_at DESC
LIMIT 20;
"
```

关键提醒：

- 当前代码在“未真正连上 Azure”时也会写本地 payload
- 所以 **不能只看 `retrieval_sync_status=READY`**
- 还必须确认：
  - 服务日志里没有 Azure upsert 异常
  - Azure Search 侧确实能查到新 chunk

通过标准：

- 至少 1 个 paragraph chunk、1 个 table-slice 相关 chunk 成功写入云端索引
- RBAC 路径 `azure_sdk` 可用
- 没有因为 embedding deployment 配置不齐而静默失败
- 当前基线：`scripts/azure_rag_smoke.py` 已确认 paragraph/table 两类知识文档都能上云，并可从 Azure Search 看到新 chunk

### Step 4. 再跑 retrieval + `llm_shadow`

目标是跑通 `AI-03`。  
这一步开始用真实 knowledge docs 对 requirement 做完整分析。

优先直接复跑脚本：

```bash
cd '/Users/liuchang/Documents/New project/prototype'
PYTHONPATH=. .venv/bin/python scripts/azure_rag_smoke.py
```

若要直接跑真实 benchmark，统一用这一种入口：

```bash
cd '/Users/liuchang/Documents/New project/prototype'
PYTHONPATH=. .venv/bin/python scripts/azure_rag_smoke.py \
  --sor-file 'path/to/real_sor.pdf' \
  --knowledge-file 'path/to/knowledge_a.pdf' \
  --knowledge-file 'path/to/knowledge_b.txt' \
  --reference-file 'path/to/reference_table.xlsx' \
  --project-name 'Azure RAG Benchmark'
```

说明：

- `--knowledge-file` 可重复传多个 standard knowledge docs
- `--reference-file` 可重复传多个 reference docs
- 若不想让脚本再额外生成一份 demo table reference，传 `--skip-generated-table-reference`
- 脚本会按文件名自动推断上传 MIME，不再固定成 `text/plain`
- 若先做最小可信 benchmark，追加 `--max-requirements 10`，脚本会在 extraction 后保留均匀抽样的少量 eligible requirements，再执行 AI analysis

验证方式：

- 保持：
  - `REQPILOT_AI_ASSESSMENT_MODE=llm_shadow`
  - `REQPILOT_RETRIEVAL_BACKEND=azure_sdk`
- 在已有 knowledge docs 的项目上重新执行 `运行 AI 分析`

UI 通过标准：

- `INFO` 行仍只显示标签，不进入判定
- `REQUIREMENT` 行开始出现 `ai_decision + ai_reason`
- 右侧 `AI 结果` 列无异常空白

数据库验证：

```bash
cd '/Users/liuchang/Documents/New project/prototype'
sqlite3 storage/app.db "
SELECT requirement_id, ai_decision, analysis_label, assessment_mode, model_name, prompt_version, updated_at
FROM assessments
ORDER BY updated_at DESC
LIMIT 20;
"
```

```bash
cd '/Users/liuchang/Documents/New project/prototype'
sqlite3 storage/app.db "
SELECT mode, retrieval_backend, model_name, validation_status, fallback_reason, created_at
FROM assessment_runs
ORDER BY created_at DESC
LIMIT 40;
"
```

通过标准：

- `assessment_runs` 中能看到：
  - `screening_llm_shadow`
  - `heuristic`
  - `llm_shadow`
- requirement evidence 非空，且来源可追溯
- `llm_shadow` 失败时，heuristic fallback 仍能兜住
- 当前基线：最近一次脚本结果已是 `overall_pass: True`，`llm_shadow` 不再回退 `evidence_reference_out_of_scope`
- 当前脚本入口额外验证：显式 `STANDARD + REFERENCE` 输入、自定义 project name、关闭内置 table reference 时也已通过
- 当前最小可信真实 benchmark 基线：`rag_real_benchmark_sample10_gpt54` 已通过，`532 -> 10` 条 requirement 抽样、`ai_processed_requirements=4`、evidence rows `12`

### Step 5. 最后再考虑 `llm_primary`

只有在以下条件都满足时，才考虑从 `llm_shadow` 切到 `llm_primary`：

- screening 没有大面积误杀真实 requirement
- retrieval 能稳定返回 evidence
- 没有引用不存在知识文档的幻觉
- 至少 10 条真实 requirement 人工抽查无阻塞问题

如果以上任一项不满足，就继续停留在 `llm_shadow`。

## 7. 重点检查点

优先看这几个地方：

- UI：
  - [http://127.0.0.1:8000/workspace](http://127.0.0.1:8000/workspace)
  - [http://127.0.0.1:8000/workspace?stage=review](http://127.0.0.1:8000/workspace?stage=review)
- 服务状态：
  - `prototype/status.sh 8000`
- 数据库：
  - `prototype/storage/app.db`
- 本地 trace：
  - `prototype/storage/ai_traces`
- 本地 knowledge payload：
  - `prototype/storage/knowledge_index`

特别注意：

- `assessment_runs` 才是最可靠的审计面
- `workspace` 页面主要用来做人工观感确认
- `retrieval_sync_status=READY` 不能单独当成 Azure upsert 成功的证据

## 8. 常见失败优先排查项

1. Azure OpenAI 能调通 chat，但没配 embedding deployment
2. `REQPILOT_RETRIEVAL_BACKEND` 仍停在 `local`
3. `REQPILOT_AZURE_AUTH_MODE` 没设成 `rbac`，结果跑到了错误路径
4. 机器上存在错误租户的 VS Code / shared token cache，若代码回退到默认链路，可能再次抢占正确 CLI token
5. 没有 `az login`，而且又禁用了 interactive login
6. Azure Search index / semantic config 名称填错
7. 只验证了 UI，没有核对 `assessment_runs`

## 9. 完成后必须回写的文档

Azure 联通或切换状态发生变化后，至少同步更新：

1. [`/Users/liuchang/Documents/New project/docs/active-context.md`](/Users/liuchang/Documents/New%20project/docs/active-context.md)
2. 当前 track 对应的 `prototype/docs/*-active-context.md`
3. [`/Users/liuchang/Documents/New project/docs/开发执行清单.md`](/Users/liuchang/Documents/New%20project/docs/%E5%BC%80%E5%8F%91%E6%89%A7%E8%A1%8C%E6%B8%85%E5%8D%95.md)
4. 如状态有实质变化，再更新 [`/Users/liuchang/Documents/New project/docs/项目.md`](/Users/liuchang/Documents/New%20project/docs/%E9%A1%B9%E7%9B%AE.md)

## 10. 一句话交接

下一个 agent 不需要再补本地非 Azure 能力，也不需要再从零打通 Azure；直接沿用当前 smoke / `sample10` 绿色基线，做人工抽查或扩大真实样本 benchmark 即可。
