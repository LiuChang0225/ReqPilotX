# 本地运行：原生 PostgreSQL、无 Docker

ReqPilotX 本地模式不会访问阿里云 RDS 或 OSS。数据库使用本机 PostgreSQL，对象存储使用私有本地目录；只有显式设置 `REQPILOT_ENV=cloud` 才会启用 OSS。

下列一次性准备用于新机器或本机重建。使用受支持的 PostgreSQL 安装；没有
PostgreSQL 时，应用会明确启动失败，绝不会连接阿里云或回退到 SQLite。

## 一次性准备

1. 安装 Node.js `>=22.12`。
2. 安装并启动 Postgres.app、Homebrew PostgreSQL 或 PostgreSQL 官方发行版。
3. 创建数据库：

   ```bash
   createdb reqpilotx
   ```

4. 在 `prototype/` 下创建虚拟环境并安装依赖：

   ```bash
   python3 -m venv .venv
   .venv/bin/pip install -r requirements.txt
   ```

5. 按需从 `.env.example` 复制配置到你自己的 shell 或密钥管理工具。不要提交真实密钥。

默认连接为 `postgresql://localhost/reqpilotx`。需要自定义时设置：

```bash
export REQPILOT_DATABASE_URL='postgresql://127.0.0.1/reqpilotx'
export REQPILOT_OBJECT_ROOT='/absolute/private/path/reqpilotx-runtime'
export REQPILOT_SCRATCH_ROOT='/absolute/private/path/reqpilotx-runtime'
```

## 启动

### App Server 运行与指定任务恢复

2026-09-10新增停止任务的二进制更新核验入口：`prototype/scripts/revalidate_native_task_runtime.py`。调用需提供当前仓库task-root、本机API、配置的codex-bin/codex-home及原因；先核对实际任务状态、来源和无未完成能力升级，再在任务/session锁内进行同线程只读检查。它只更新已验证的新binary pin并记录前后证据，保留历史回执/Map/确认，不重启模型、不清掉历史FAILED记录。运行/排队、前版或来源不匹配、模型/home变化、协议/PDF约束验证失败时拒绝修改。能力包变更仍走原有capability epoch，不能用重签身份绕过差异。

应用的 Agent 入口需要 `REQPILOT_AGENT_RUNTIME=codex-app-server`、
`REQPILOT_CODEX_BIN` 和专用的 `REQPILOT_CODEX_HOME`。凭据库由
`REQPILOT_CODEX_AUTH_STORE=file|keyring|auto` 显式选择，默认仍为 keyring。
应选择专用 home 已登录使用的存储；配置不会登录、复制凭据或读取其他项目的认证。
运行前按检出版本的协议与能力锁核验二进制、schema、Skill、plugin manifest、Python 和渲染器身份。历史开发机版本不代表新机器已满足条件。

首次 PDF 的生产入口是
`POST /api/projects/{project_id}/sor-documents/{document_id}/agent-structure/document-map/start`，
输入 `command_id` 与 `expected_source_sha256`，返回持久化 job_id；对应 `/status`
返回进度、失败原因和真实 COMPLETE/PARTIAL 结果。Agent 的整文调查、章节判断、
正文/表格/图像归属消费现有 Agent Kit 和验证器，发布到现有 working Document Map。
已有原工作台直接读取该 PostgreSQL／对象存储指针。非 PDF 或缺少可恢复结构证据时
必须保留明确的未完成状态，不会伪造章节、原文或成功。

从仓库根可调用同一业务入口：

```bash
REQPILOT_CODEX_AUTH_STORE=file prototype/.venv/bin/python prototype/scripts/agent_workflow.py doctor
REQPILOT_CODEX_AUTH_STORE=file prototype/.venv/bin/python prototype/scripts/agent_workflow.py start \
  --project-id PROJECT --document-id DOCUMENT --source-sha256 SHA --command-id COMMAND
REQPILOT_CODEX_AUTH_STORE=file prototype/.venv/bin/python prototype/scripts/agent_workflow.py run-job --job-id JOB
```

`run-job` 只领取指定的现有任务并跳过全局维护，不消费其他历史任务；常规 worker 也可用
`--once --job-id JOB`。相同 command_id 幂等，不会重复创建模型任务。失败后的自动重试
使用同一来源/Kit绑定的对象存储检查点；终态任务可通过 start 的 `--resume-job-id JOB`
显式续接。同一来源版本和 Kit 必须匹配。模型 turn 完成与文档 COMPLETE 分开：
未解决范围保留 PARTIAL，CLI 对未完整成功返回非零值。源文本/候选/检查点留在私有
ObjectStore，公共状态记录运行身份、阶段、来源哈希和模型调用数量。

地图发布后，`POST .../agent-structure/requirement-baseline/all-runs/start` 接受
`command_id`、`expected_source_sha256`、`expected_artifact_id`，只枚举当前地图尚未处理的
有正文节点、逻辑表和原图范围，调用同一 SourceItem 正文／表格／图像发布链。CLI 对应
`source-items-start --project-id PROJECT --document-id DOCUMENT --source-sha256 SHA
--artifact-id ARTIFACT --command-id COMMAND`。返回 `jobs` 后逐个以指定 job_id 运行；
标题原文也交给 D3 判断是否承载义务，已有范围不重复计算。首次来源使用明确的 EMPTY 原文账本，
SRC/REQ 从第一条真实发布记录开始签发，不创建占位需求。分类 schema、窗口执行器与
Agent Kit 共用 FUNCTIONAL／QUALITY／CONSTRAINT／PROJECT／PROCESS 五种类别。

只运行新工作台入口时，在持续运行的终端启动 `prototype/scripts/agent-workbench-worker.sh start`。
该命令以前台进程常驻，`stop` 对匹配的 PID 请求排空；终端应保持打开或交由系统进程管理器托管。
它仅领取 `application_entrypoint=source-item-workbench-v1` 的地图、原文、分类、图像、
显式拆分和分析任务，连同过期租约恢复也使用这一过滤器，不领取旧队列或执行全局维护。
新图像任务的 `dependency_job_ids` 由入口绑定本次同源表格任务；任一前置缺失或未成功时，
普通领取和指定 job 领取都保持等待且不增加 attempts，失败的表格任务需要恢复后再继续。
地图入口可传 `auto_source_items=true`，地图发布后会在同一持久任务系统持续入队后续原文范围。

原文分类 packet 带完整章节正文、关联表头／表注／工况，产品验证方法和条件随其功能或质量
目标判断，不能仅凭“验证活动”归为 PROCESS。原图使用核验哈希的原页输入，视觉释读独立保存，
不伪装逐字原文。图像、分类和正文后继共用一个基线版本序列与发布验证器。

显式拆分入口 `.../agent-structure/requirement-baseline/decomposition/start` 与证据分析入口
`.../agent-structure/requirement-baseline/analysis/start`
使用实际 App Server turn，保存输入快照、调用回执和修复尝试。分析默认读取当前 working 拆分，
逐条绑定真实项目知识 fact 的版本、SHA、原文引用和适用工况；缺证据或准则不明保留待补证／待澄清。
同一来源、拆分和知识版本的局部分析按 unit 合并；模型完成后的失败恢复也重新合并最新未选条目，
不重复调用模型。来源或知识版本已变时不会继承旧结论。`PARTIAL` 与待补证结果均保留实际含义。

```bash
cd prototype
./start.sh
```

启动脚本先按 `package-lock.json` 同步 React/Vite 依赖，并在前端源码新于
`frontend/dist` 时执行 TypeScript + Vite production build；随后按
`requirements.txt` 的 SHA-256 检查现有虚拟环境，应用版本化 PostgreSQL
migration，再同时启动 API 与有界 durable worker 池。数据库不可用时会明确退出，
不会安装系统软件，也不会回退 SQLite。默认两个隔离进程槽位：通用槽处理完整队列，
第二槽只领取冻结方案为 `llm_provider=none` 的 `document.extract`；可用
`REQPILOT_LOCAL_WORKER_COUNT=1|2` 下调，但不能超过 2。同一文档的
extract/restore/optimize 仍由 PostgreSQL claim 互斥，knowledge sync 仍只有通用槽。

每个槽位有独立 PID、screen 和日志。普通 `start.sh` 要求现有 API/worker 的源码
revision、实例、端口和槽位合同完全一致且槽位完整；缺槽时也拒绝用当前 shell 的
数据库/对象配置补进既有实例，必须成对重启。
无损升级使用 `./start.sh --drain-restart`；也可先 `./stop.sh --drain`，观察
`./status.sh`，待 worker 完成在手任务后再重启。排空只发 SIGTERM，不执行超时 SIGKILL；
信号与 claim 竞态时，尚未执行的 lease 会 CAS 退回 PENDING。

```bash
./status.sh
./stop.sh
./stop.sh --drain
./start.sh --drain-restart
./start.sh --dev  # Vite 5173 + API 8010 + 同样的有界 worker 池
```

### 历史 PUBLISHING 空租约恢复

`0020_requirement_set_publication_leases.sql` 之前遗留的 `PUBLISHING` 行没有
可信时间戳，普通请求会 fail closed，不会按创建时间猜测为过期。只有确认具体
Requirement Set 是历史半成品时，才允许显式恢复。先用 `./stop.sh` 停止 API
和 worker，并确认 `./status.sh` 中两者均未运行；随后传入精确的项目、文档、
Requirement Set 和 publication key：

```bash
.venv/bin/python scripts/runtime_admin.py recover-historical-publication \
  --project-id '<project-id>' \
  --document-id '<document-id>' \
  --requirement-set-id '<requirement-set-id>' \
  --publication-key '<64位小写sha256>' \
  --confirm RECOVER_HISTORICAL_EMPTY_PUBLICATION_LEASE
```

该命令只把精确匹配的历史空租约转换为完整但已过期的 fence，不直接取得发布
所有权，也不删除部分行。重新 `./start.sh` 后，下一次相同内容的普通发布请求会
在同一 Requirement Set、版本和 publication key 上通过正常 CAS 接管并重放；
活动、已完成、已挂树或已有租约的行不会被该命令修改。

## 旧数据一次性迁移

已有 PostgreSQL 环境不要重复从 SQLite 覆盖。以下命令只用于确实持有历史 SQLite 输入、且目标为空的新环境首次迁移。

先停止旧进程，并在仓库和运行目录之外创建容量充足的备份：

```bash
.venv/bin/python scripts/storage_backup.py backup \
  --storage '/absolute/path/to/legacy-storage' \
  --output '/absolute/path/to/backups/reqpilotx-cutover-YYYYMMDD-HHMMSS'
```

只读预演：

```bash
.venv/bin/python scripts/sqlite_to_postgres.py \
  --source '/absolute/path/to/legacy-storage/app.db'
```

备份完成并停止旧进程后，通过 SQLite 自身完成 checkpoint 和日志模式切换；
不得手工删除 WAL/SHM：

```bash
sqlite3 '/absolute/path/to/legacy-storage/app.db' \
  'PRAGMA wal_checkpoint(TRUNCATE); PRAGMA journal_mode=DELETE;'
```

确认 `quick_check=ok`、0 个外键违规、源库没有 WAL/SHM、目标 PostgreSQL
为空且备份验证通过后，才执行：

```bash
.venv/bin/python scripts/sqlite_to_postgres.py \
  --source '/absolute/path/to/legacy-storage/app.db' \
  --apply
```

迁移是单向硬切，不做双写。新库开始写入后，不得再启动旧 SQLite 路径。

当前正式入口是 `http://127.0.0.1:8000`。`/api/runtime-readiness` 只以
PostgreSQL、LocalObjectStore 和应用启动状态判断基础运行是否可用；未配置的
可选 AI/Qdrant profile 会显示能力受限，但不会阻断本地项目、工作台、审阅
和导出。

核心运行仍不依赖 Docker。macOS/Linux 启用 Qdrant profile 并显式配置
loopback URL 后，`start.sh` 会复用健康的本地实例，或从显式路径、随包路径、
`PATH`、用户本地安装和 Homebrew 中解析 Qdrant 二进制并自动启动；不会自动
创建容器或访问云端。子进程只绑定 `127.0.0.1`，使用独立数据目录且不继承
数据库与云端凭据。`stop.sh` 只停止带有 ReqPilotX 所有权回执的进程；8000
与 8010 并行时，停止任一实例都会保留仍被另一实例共享的 Qdrant。

Windows 暂不启用 Python 自动二进制生命周期，因为当前缺少与 Unix `lsof/ps`
等价的可靠监听和进程所有权证明；该路径会在启动子进程前明确拒绝。Windows
使用 `start.ps1 -WithQdrant`，由现有 PowerShell 合同显式创建并核验仅绑定
loopback 的 Docker 容器。

### Agent 与可选处理能力

当前 AI 阅读和 Agent 文档路径使用本机 Codex App Server。运行配置为
`REQPILOT_CODEX_BIN`、`REQPILOT_CODEX_HOME`、`REQPILOT_AGENT_MODEL_ID`；
具体默认值与就绪检查以当前代码为准。必须具备批准的登录、目标模型和相应工具/来源权限，
不能因为 Azure 或百炼参数存在就显示 Codex 已就绪，也不能静默切换 provider。

知识文件可以先保存；可选检索不可用时只影响同步或依赖检索的分析，不阻断本地项目与来源保存。
旧 Azure/百炼/视觉 sidecar 配置保留为明确任务可用的兼容资产，不再作为普通运行的默认选择。
如使用会外发整页内容的视觉 adapter，必须先确认客户授权与服务范围，不能把 Base64 当作脱敏。

原文、已接受工件和历史会话在能力不足时仍应诚实可读；不得把缺少必需视觉内容或超限的输入
标记为完整理解。配置模板见 [prototype/.env.example](../../prototype/.env.example)。
当前开发主线见[三阶段计划](../需求提取/开发计划.md)，不是先补齐全部运行平台。

### Codex直接修订既有来源版本

已有Document Map/SourceItem的分类重核及原图释读可由当前Codex任务直接完成，无需伪造App Server回执或再次模型调用。使用同一持久化队列和对象存储保存后继工作版本：

```bash
prototype/.venv/bin/python prototype/scripts/agent_workflow.py direct-revision-start \
  --project-id PROJECT --document-id DOCUMENT --input '<private-direct-revision.json>'
prototype/.venv/bin/python prototype/scripts/agent_workflow.py run-job --job-id JOB
```

输入合同为`source-item-direct-revision/v1`：固定source/Map/前版SHA、明确classification decisions、预期候选SHA、可选已验证decomposition、同源visual_readings和base_annotations，以及真实`CODEX_APP_DIRECT`provenance（动作记录、时间、模型、用户授权范围）。导入重新编译与核对，不运行LLM。指定job只处理该任务；成功后主工作台读取新版本，旧版对象及前驱指针保留，未创建人工接受或符合性结论。原App Server路径继续要求真实运行与清理回执，不能混用两种身份。

`source-item-visual-readings/v1`将图片解释关联到已有文字/表格SRC；校验source SHA、Map、原条目绑定、image anchor与版本。解释通过`record.visual_interpretation`和delivery的`node.source_item.visual_interpretation`提供，始终与客户原文分开，`related_images`/`figure_refs`保留原图。后续拆分与分析可读取direct工作版本中的同源派生，不把纯图片说明创建为新REQ。
