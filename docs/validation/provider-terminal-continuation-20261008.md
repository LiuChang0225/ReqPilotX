# 终态反馈接续：本地验证记录

日期：2026-10-08。隔离目录 `/tmp/reqpilotx-terminal-feedback-20261008`，detached HEAD
`812a4f18fb5e057bf8727301893fd74665d4a046`，tree
`0206e84c1ea8b8f96da9a5eb081b5aa011f72e3a`。未提交候选以完整 patch、文件清单和 SHA 识别，
不能用 HEAD 代替改动后的完整身份。合同和真实运行数字见[架构说明](../architecture/provider-terminal-continuation.md)。

## 实际动作与文件范围

正常 fetch / ls-remote 确认发布分支 `codex/reqpilotx-v1-cloud-v2-20261001` 为指定基线；
远端 main 为 `a7d9d29e081b1be8c73e72e57bf67cbd44ec5903`。主工作树仍在
`f18cf939adc208e7b0ed72d6d4e667b52fa8ee75`。维护代码/测试七个未跟踪入口和旧
`/private/tmp/reqpilotx-qwen-boundary-restored-20261008` 现场保持原样。未 force/reset/prune。
fetch/worktree 与本机 PostgreSQL Unix socket/进程操作经沙箱许可完成，无剩余访问阻塞。

开始/结束可用磁盘约 11 GiB；checkout 约 61 MiB。复用现有 Python 3.13.2 venv、OpenAI SDK、
psycopg 3.3.4 / psycopg_pool 3.3.2 和 Homebrew PostgreSQL 16.14，无安装。清空继承环境再运行
unittest；所有来源均为临时虚构 PDF、真实编译/review/export 与 fake/mock SDK。

精确候选文件（共九个）：

| 文件（相对仓库根） | 改动 |
|---|---|
| `apps/server/src/reqpilotx_server/adapters/agents/provider_tool_loop.py` | handoff 请求投影；历史与起点证明；metadata / 解析前 usage |
| `apps/server/src/reqpilotx_server/application/agent_workflow/provider_continuation.py` | 新增保存 pin、来源/版本证明、可读完整审计副本、活动恢复复核 |
| `apps/server/src/reqpilotx_server/application/agent_workflow/source_document_loop_jobs.py` | raw-document 入队与 provider 接续 |
| `apps/server/src/reqpilotx_server/application/agent_workflow/source_item_loop_jobs.py` | review-only 入队与 provider 接续 |
| `prototype/tests/test_provider_continuation.py` | 新增 18 项离线专项 |
| `prototype/tests/test_provider_continuation_postgres.py` | 新增 1 项显式 Unix socket / UUID 临时数据库专项 |
| `docs/architecture/provider-terminal-continuation.md` | 新增合同、证据、边界、验收条件 |
| `docs/validation/provider-terminal-continuation-20261008.md` | 本记录 |
| `docs/issues/provider-tool-loop-foundation.md` | 链接本轮接续合同 |

没有修改分类规则、公开工具 schema、runtime 配置或计费系统。没有新增调用次数、token、输出
或金额限制，没有 LLM 摘要调用。未读取真实模型凭据、调用真实模型、提交、推送、同步、构建
发行或部署；不把本次范围声称为完整 SaaS 已交付。

## 最终结果

| 验证 | 结果 |
|---|---|
| Server/provider/native/source/workbench 19 模块，含两个真实 PostgreSQL 探针 | 172 项：172 通过，0 失败，0 跳过；12.090 秒 |
| Portable review loop | 27 项全过，0 跳过；3.220 秒 |
| 总计（不重复计独立审查） | 199 项通过，0 失败，0 跳过 |
| 独立 agent 回归 | 56 项全过，0 跳过 |
| 本地 / 独立 AST 不变量 | 12 项 / 10 项通过 |
| diff 空白、patch 正向 cached check / 反向 working-tree check | 通过；未 stage 或 apply |

两个 PostgreSQL 探针使用新建的 `/tmp/rpx-cont-pg-20261008` 集群、
`/tmp/rpx-cont-pg-20261008/socket` Unix socket、端口 65431，`listen_addresses=''`，
trust 认证仅限该专用临时实例。各探针创建 UUID 数据库，应用 39 个既有迁移，finally 删除数据库。
最终只读查询确认只剩 `postgres` 和模板库，TCP 未开放；集群已停止，临时目录已删除，日志保留。
没有接触业务库或读取其凭据。

- 既有发布探针：同前版并发仅一份成功；初始图谱、图谱/基线、同基线 review 竞态，manual
  publication transaction / rollback 等既有 fence 场景通过。
- 新接续探针：真实队列 enqueue/claim/succeed 保存结果；真实私有对象指针及哈希；重开数据库
  连接池后仍固定同一 predecessor；重复入队幂等；durable CANCELLED 后无新 pin 的活动恢复；
  源漂移拒绝；待配对工具 ID/结果和 Qwen reasoning、原始 snapshot 与保存结果保留。

合成测量仍为 **82 条 / 329,210 字节 → 3 条 / 5,679 字节**。口径为消息 JSON UTF-8，
不含工具 schema、HTTP envelope 或 SDK 编码。fixture 是虚构内容、35 批次/43 调用/36 响应，
无图像。这只证明结构变化，不证明真实 token、模型质量、缓存、真实费用下降；没有冒充用户的
84/130 条真实消息记录，也没有把 usage 当成账单。

## 独立审查与失败记录

独立 agent 只读审查实现、终态证明、保存状态和工具链，另建临时 fixture / fake SDK 反例。
报告 `/tmp/reqpilotx-terminal-feedback-evidence/independent-review.md`，SHA256
`997e6a9b71781d04b4bba94b99247f7d7b85a67962e30245044ff4e762deda5d`。

| 发现 | 处理与复核 |
|---|---|
| P1：活动恢复只核对 handoff digest，原件/基线/审计/保存确认漂移仍可继续 | 恢复前复核固定文件、原始 snapshot/receipt/calls、durable saved predecessor 与图谱；有/无 pin 同查。六类独立反例全部拒绝，待配对工具回放 0 次；mutable review 允许合法 steering |
| P2：review-only 任意同名 SourceIR 被当前打 hash 当作来源证明 | 无 producer ledger 时拒绝常见 SourceIR 文件，本范围不扩展 provenance 协议；独立反例拒绝，raw producer 固定哈希保持 |

独立修后未发现其他可操作问题；56 项回归和 10 项 AST 通过。审查实现 SHA 为
`7081bf8db8d1de038f60a4de5f515b945e556604b6697a88e5739fecf2ce3c0c`。
这是另一 agent 的审查，不是外部人工或真实模型验收。

保留实际失败：早期投影漂移 fixture 用错首次状态导致 KeyError，修正后通过；独立反例脚本首跑
漏 repository sys.path，修正后运行；初次 initdb 在沙箱内 shmget 被拒，权限获准后成功；首次
pg_ctl 因 locale 导致 postmaster multithreaded 错误，清空环境并设置 `LC_ALL=C TZ=UTC` 后启动；
首次 Unix socket 连接被沙箱拒绝，专用实例权限获准后通过；本轮聚焦命令曾写错 thinking 模块名，
该命令总体失败，改为 `test_provider_thinking_compatibility` 后 27 项通过。均未改写为首次全过。

## 完整复跑与本地工件

候选根执行；本机所有命令复用现有 Python，不装依赖。完整可运行脚本在证据目录：

```bash
bash /tmp/reqpilotx-terminal-feedback-evidence/rerun-offline.sh \
  /tmp/reqpilotx-terminal-feedback-20261008 \
  /tmp/reqpilotx-terminal-feedback-rerun-evidence
```

脚本展开的集群与测试命令（新的临时路径由 mktemp 产生；trap 停止并删除自己创建的集群）：

```bash
validation_python=${REQPILOT_VALIDATION_PYTHON:?Set to an existing venv Python}
test_pg_root=$(mktemp -d /tmp/reqpilotx-continuation-pg.XXXXXX)
mkdir -m 700 "$test_pg_root/socket"
env -i PATH=/usr/bin:/bin:/opt/homebrew/bin LC_ALL=C TZ=UTC \
  /opt/homebrew/bin/initdb -D "$test_pg_root/data" -U rpx_continuation_test \
  --auth=trust --locale=C --encoding=UTF8
env -i PATH=/usr/bin:/bin:/opt/homebrew/bin LC_ALL=C TZ=UTC \
  /opt/homebrew/bin/pg_ctl -D "$test_pg_root/data" -l "$test_pg_root/postgres.log" \
  -o "-c listen_addresses='' -c unix_socket_directories='$test_pg_root/socket' -p 65431" -w start

env -i PATH=/usr/bin:/bin:/opt/homebrew/bin LC_ALL=C TZ=UTC \
  PYTHONPATH=prototype:apps/server/src:packages/engine/src \
  REQPILOT_RUN_NATIVE_LOOP_POSTGRES_E2E=1 REQPILOT_RUN_CONTINUATION_POSTGRES_E2E=1 \
  REQPILOT_POSTGRES_INTEGRATION_URL="host=$test_pg_root/socket port=65431 user=rpx_continuation_test dbname=postgres" \
  "$validation_python" -m unittest \
  prototype.tests.test_provider_continuation \
  prototype.tests.test_provider_continuation_postgres \
  prototype.tests.test_provider_tool_loop \
  prototype.tests.test_provider_thinking_compatibility \
  prototype.tests.test_source_document_loop_jobs \
  prototype.tests.test_source_item_loop_jobs \
  prototype.tests.test_native_loop_finalization \
  prototype.tests.test_native_loop_driver \
  prototype.tests.test_native_loop_history \
  prototype.tests.test_native_loop_tools \
  prototype.tests.test_native_loop_workspace \
  prototype.tests.test_native_loop_pdf_reading \
  prototype.tests.test_native_loop_progress \
  prototype.tests.test_native_loop_capabilities \
  prototype.tests.test_native_loop_publication_postgres \
  prototype.tests.test_provider_window_executors \
  prototype.tests.test_provider_joint_mixed_source_jobs \
  prototype.tests.test_plugin_workbench \
  prototype.tests.test_plugin_workbench_delivery -q

env -i PATH=/usr/bin:/bin:/opt/homebrew/bin \
  PYTHONPATH=prototype:apps/server/src:packages/engine/src \
  "$validation_python" -m unittest discover -s plugins/reqpilot/tests -p 'test_review_loop.py' -v

env -i PATH=/usr/bin:/bin:/opt/homebrew/bin \
  PYTHONPATH=prototype:apps/server/src:packages/engine/src \
  "$validation_python" /tmp/reqpilotx-terminal-feedback-evidence/independent-review-counterexamples.py
env -i PATH=/usr/bin:/bin:/opt/homebrew/bin "$validation_python" \
  /tmp/reqpilotx-terminal-feedback-evidence/independent-review-static.py
env -i PATH=/usr/bin:/bin:/opt/homebrew/bin "$validation_python" \
  /tmp/reqpilotx-terminal-feedback-evidence/boundary-review.py

env -i PATH=/usr/bin:/bin:/opt/homebrew/bin LC_ALL=C TZ=UTC \
  /opt/homebrew/bin/pg_ctl -D "$test_pg_root/data" -m fast -w stop
rm -rf "$test_pg_root"
```

证据目录 `/tmp/reqpilotx-terminal-feedback-evidence/` 保留 server/review/独立审查日志、复现脚本、
AST proof、两个数据库 proof、集群只读与停止证据。`candidate.patch` 包含以上九个候选文件，
`delivery-manifest.json` 固定基线、完整文件 SHA、测试和证据 SHA；`delivery.sha256` 记录补丁与
清单 SHA。生成器 `build-delivery.py` 可复跑。patch 不含合成来源、运行数据、客户内容或凭据。

## 剩余边界

无本地实现或离线验证阻塞。完整 SaaS、真实模型质量、缓存/费用和部署环境仍需另行授权验收。
review-only SourceIR provenance 与活动大载荷引用协议为独立后续范围。本轮不重构工具/API，
下一次有界真实验收需要的来源、版本、handoff、完整私有审计、逐请求 metadata / usage 与质量
核对证据见[合同末段](../architecture/provider-terminal-continuation.md)。全部结果保持本地。
