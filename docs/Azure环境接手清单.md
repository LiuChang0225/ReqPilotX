# Azure 环境接手清单

更新时间：2026-03-31 10:29 CST  
适用仓库：`/Users/liuchang/Documents/New project`

这份文档给“切换到公司网络后的下一个 agent”使用。  
目的只有一个：在不重做本地工作、不重新摸索上下文的前提下，把 Azure 侧联通和验证一次性跑通。

长期背景：[`/Users/liuchang/Documents/New project/docs/项目.md`](/Users/liuchang/Documents/New%20project/docs/%E9%A1%B9%E7%9B%AE.md)  
执行总表：[`/Users/liuchang/Documents/New project/docs/开发执行清单.md`](/Users/liuchang/Documents/New%20project/docs/%E5%BC%80%E5%8F%91%E6%89%A7%E8%A1%8C%E6%B8%85%E5%8D%95.md)

## 1. 当前基线

- 当前 `main` 上，本地非 Azure 路径已收口到可用状态
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
- 当前唯一主 blocker：公司网络下的 Azure OpenAI / Azure AI Search live 验证

## 2. 接手前先看

按这个顺序看，不要先翻旧 PRD：

1. [`/Users/liuchang/Documents/New project/docs/项目.md`](/Users/liuchang/Documents/New%20project/docs/%E9%A1%B9%E7%9B%AE.md)
2. [`/Users/liuchang/Documents/New project/docs/开发执行清单.md`](/Users/liuchang/Documents/New%20project/docs/%E5%BC%80%E5%8F%91%E6%89%A7%E8%A1%8C%E6%B8%85%E5%8D%95.md)
3. [`/Users/liuchang/Documents/New project/docs/active-context.md`](/Users/liuchang/Documents/New%20project/docs/active-context.md)
4. [`/Users/liuchang/Documents/New project/prototype/docs/extraction-active-context.md`](/Users/liuchang/Documents/New%20project/prototype/docs/extraction-active-context.md)
5. 本文

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

### Step 4. 再跑 retrieval + `llm_shadow`

目标是跑通 `AI-03`。  
这一步开始用真实 knowledge docs 对 requirement 做完整分析。

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
4. 没有 `az login`，而且又禁用了 interactive login
5. Azure Search index / semantic config 名称填错
6. 只验证了 UI，没有核对 `assessment_runs`

## 9. 完成后必须回写的文档

Azure 联通或切换状态发生变化后，至少同步更新：

1. [`/Users/liuchang/Documents/New project/docs/active-context.md`](/Users/liuchang/Documents/New%20project/docs/active-context.md)
2. [`/Users/liuchang/Documents/New project/prototype/docs/extraction-active-context.md`](/Users/liuchang/Documents/New%20project/prototype/docs/extraction-active-context.md)
3. [`/Users/liuchang/Documents/New project/docs/开发执行清单.md`](/Users/liuchang/Documents/New%20project/docs/%E5%BC%80%E5%8F%91%E6%89%A7%E8%A1%8C%E6%B8%85%E5%8D%95.md)
4. 如状态有实质变化，再更新 [`/Users/liuchang/Documents/New project/docs/项目.md`](/Users/liuchang/Documents/New%20project/docs/%E9%A1%B9%E7%9B%AE.md)

## 10. 一句话交接

下一个 agent 不需要再补本地非 Azure 能力，直接去公司环境按“screening -> index upsert -> retrieval + llm_shadow”的顺序完成 Azure live 验证即可。
