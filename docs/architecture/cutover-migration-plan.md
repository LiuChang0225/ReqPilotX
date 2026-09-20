# ReqPilotX 收口与硬切迁移计划

更新时间：2026-07-28

当前执行状态：**本地代码、数据和正式入口硬切均已完成，已停在阿里云资源操作之前。** 本机现在使用 PostgreSQL 16.14 + LocalObjectStore + 独立 worker；旧 SQLite 已冻结。当前没有阿里云资源凭据，因此 RDS/OSS 恢复、SAE 发布和云端 failover 仍未执行，不能用本地结果替代。

## 1. 本轮要收口什么

| 领域 | 当前状态 | 收口结果 |
|---|---|---|
| 产品入口 | prepare/new Agent 与 legacy Requirement preview 并存 | 只有 prepare → DRAFT → review |
| HTTP | `main.py` 约 12k 行、路由与运行装配混合 | app factory + routers + application services |
| 数据 | SQLite 50 MiB 本地库、多处 adapter 自建表 | PostgreSQL 唯一运行数据库 |
| 任务 | daemon thread、进程字典、文件 checkpoint | PostgreSQL durable job + 独立 worker |
| 文件 | DB 保存绝对路径，API/解析器直接读本机文件 | storage key + LocalObjectStore/OSS |
| 安全边界 | 只允许 loopback，无法部署 | local/cloud 显式 Host/Origin 策略 |
| 部署 | Azure/App Service 与桌面发布文档混杂 | 阿里云 SAE + RDS + OSS；本地无 Docker |
| 门禁 | Demo/formal/Silver/Human/性能叠成开发前置 | 提交快检、主流程回归、发布验收三层 |

## 2. 执行波次

### Wave A：合同与基础设施

- [x] 批准唯一目标架构和无双路径原则。
- [x] 增加 runtime settings、cloud-safe request boundary 和 app factory。
- [x] 增加 ObjectStore port、Local/OSS adapter。
- [x] 增加 PostgreSQL connection/migration runner 和 durable job repository。
- [x] 增加独立 worker entrypoint。
- [x] 将公开 `extraction-shadow` URL 改为 `extraction`，旧 URL 返回 404。
- [x] 将 70 条正式 API 拆入 8 个领域 router，并把 `main.py` 收缩为薄 ASGI 入口。
- [x] worker handler 改为直接依赖 application，不再 import HTTP composition。
- [x] 删除 `application/workspace.py` 与全局 contracts 聚合，按领域拆分 use case/请求合同。
- [x] application 移除 FastAPI 类型，由 API adapter 统一做传输映射。

完成标准：基础设施契约单测通过；本地 dry-run 不访问云；API 可在 test profile 装配。

### Wave B：数据硬迁移

- [x] 冻结第一版 PostgreSQL schema（0001–0004）。
- [x] 对 SQLite 源执行只读 inventory、foreign-key check 和内容摘要。
- [x] 创建并验证带时间戳、不可覆盖的 SQLite + 文件存储本机备份。
- [x] 迁移所有业务表、review/enrichment/SourceIR/provider-run 表。
- [x] 实现 uploads、knowledge、exports、table snapshots 和运行缓存的对象 key 迁移计划。
- [x] 实现每表行数、主键摘要、全行摘要、外键与关键业务不变量校验。

完成标准已满足：迁移 rehearsal 可重复；源业务数据不变；目标校验报告不含客户正文。本机备份与源仍在同一物理磁盘，产品负责人已接受它作为当前阶段的恢复边界，不再安排异盘或远端复制。

### Wave C：调用者切换

- [x] `db.py` 在目标运行时改走 PostgreSQL repository facade。
- [x] review、enrichment 和 assessment 的生产持久化切 PostgreSQL；SQLite 类仅供测试/迁移。
- [x] 上传、下载、来源裁剪、导出和删除切 ObjectStore。
- [x] LLM extraction、restoration、assessment、knowledge sync、export 切 durable jobs。
- [x] knowledge 上传不再同步解析；正式 worker 物化对象并以 SourceIR/严格 TXT 解码持久化检索文本后再建索引。
- [x] PostgreSQL 生产分支不再启动任务线程或把进程字典作为 job truth；同步 helper 只供隔离测试。
- [x] 测试、桌面入口和诊断脚本改为显式 application import，不再借 `app.main` 访问业务函数。

代码完成标准已经满足：调用者只有一条生产路径，架构契约与完整本地测试通过。真实 PostgreSQL 已验证 API/worker 重启、两个 worker 使用 `SKIP LOCKED` 分别 claim、同键 enqueue 幂等复用和 DRAFT export durable publication；取消与 lease 超时的细节继续由聚焦回归覆盖，云端 failover 留在发布验收。

代码分域、正式入口和生产持久化可达性已经满足云迁移前的 composition 前置条件。PostgreSQL adapter 不再继承 SQLite adapter，生产记录不再接受机器绝对路径，非测试环境初始化失败会直接终止启动。真实 PostgreSQL、双 worker 和 LocalObjectStore 验证已完成；RDS、OSS 与 SAE 尚未触碰。

### Wave D：产品硬切

- [x] Review 删除 raw preview fallback。
- [x] 删除 `/extraction-preview`、`/refresh-preview`、同步 `/analyze`、同步 AI 和 `extraction-shadow` API。
- [x] 删除产品可达的 legacy parser/serving fallback、feature flag 与双链文案。
- [x] 正式上传只做来源 preflight/对象发布，不在 HTTP 请求中同步运行兼容 parser。
- [x] 默认启动入口改为 app factory + worker。
- [x] SQLite adapter 与旧解析 helper 只在显式 test/cutover 范围可达，不进入生产 composition。
- [x] 删除 Phase0/进度看板静态页面，正式浏览器入口统一到 `/workspace` 主链。

完成标准：正式 router/worker composition 不包含 legacy/shadow route、同步 fallback 或 SQLite adapter；旧 URL 404；核心 smoke 只命中新链。

产品路径、入口与浏览器工程化硬切均已完成。React 19 + Vite 8 + TypeScript
是唯一前端源码和构建入口；API client、状态和领域模块已经分域，旧
`app/static` 已删除。

### Wave E：正式上线前准备与阿里云发布

- [x] 完成 React/Vite/TypeScript 单一前端、领域拆分、正式构建和旧静态入口删除。
- [ ] 完成发布级目标浏览器、多视口和大文档性能回归。
- [ ] 在 RDS 建库并应用同一 migration。
- [ ] 创建私有 OSS bucket、RAM 最小权限和生命周期策略。
- [ ] 从当前 PostgreSQL/LocalObjectStore 一致性迁移到 RDS/OSS；不得从冻结 SQLite 生成“最新”云数据。
- [ ] 部署 SAE API/worker，配置 VPC、health check、secret 和允许域名。
- [ ] 在阿里云环境执行 migration rehearsal、核心 smoke、备份恢复和 worker failover。
- [ ] 仅在正式启用对应 AI/RAG profile 时确定 Qdrant 云端形态；本地 macOS Docker / Windows 二进制不影响核心发布链。

完成标准：目标环境核心 smoke、数据恢复和回滚演练通过；不要求本地 Docker。

## 3. SQLite → PostgreSQL 数据迁移

### 3.1 迁移前

1. 停止 API/worker 写入。
2. 运行 SQLite `PRAGMA quick_check`、`foreign_key_check`。
3. 使用现有备份工具生成不可覆盖备份并记录 SHA-256。
4. 记录表名、行数、主键集合摘要、对象文件数/总字节。
5. PostgreSQL 应用空库 migration。

### 3.2 迁移

- 按外键拓扑批量复制；ID、时间字符串、JSON payload 和 append-only revision 原样保留。
- SQLite `INTEGER` 布尔字段先保留为 integer，避免迁移时改变业务解释；后续独立 migration 才可转 boolean。
- 文件先写对象存储并复核 hash，再写 `storage_key`。
- 活动进程内任务不得伪造续跑：迁为 `INTERRUPTED` 并生成显式 rerun action。
- 迁移程序使用幂等 migration run ID；重复执行只能复核或恢复，不重复插入。

### 3.3 迁移后校验

- 每表行数、主键集合摘要一致；
- 所有外键、唯一索引和 active-set/active-job 约束成立；
- Requirement Set version/active pointer、review revision、publication order 和 decision lineage 一致；
- 每个 storage key 可读，size/hash 与数据库一致；
- 核心 `prepare -> publish DRAFT -> selected review -> export DRAFT` smoke 通过；
- SQLite 与原文件保持只读封存。

## 4. 切换和回滚

切换窗口：

1. 停写并创建最终备份；
2. 执行 final migration；
3. 启动 worker，再启动 API；
4. 执行只读 smoke；
5. 开放写入。

不提供运行时双写。开放新写入前可以回到旧版本 + 最终 SQLite/文件备份。开放新写入后，旧 SQLite 已经落后；此时回滚只能恢复同一时点的 PostgreSQL/OSS 备份或 fixed-forward，不能直接启动旧路径，否则会丢数据。

## 5. 本地无 Docker

本地只需要：

```text
Postgres.app 或 Homebrew PostgreSQL
Python venv
LocalObjectStore 目录
API 进程
Worker 进程
```

默认配置不得连接 RDS/OSS。若没有 PostgreSQL，启动应给出一次性安装/建库提示并退出，不自动安装系统软件、不静默回退 SQLite。

这里的“无 Docker”指核心运行时不依赖 Docker。可选 Qdrant 在 macOS 可用
Docker，在 Windows 可用二进制；它不参与 PostgreSQL、LocalObjectStore、
工作台、审阅和导出的核心 readiness。

## 6. 精简门禁

### 提交级，阻断

- Ruff/编译；
- 受影响模块单测；
- migration schema smoke；
- 正式 API 路由合同；
- 一条核心工作流 smoke；
- secret/source/data-integrity 检查。

### 定时回归，非阻断普通开发

- 完整单测；
- 29-case corpus；
- 模型重复性、Silver、AI pre-review；
- UI 多视口和大文档资源基线。

### 发布级，阻断发布

- 目标平台 migration rehearsal；
- RDS/OSS 备份恢复；
- worker lease/failover；
- Human/formal 业务验收；
- 阿里云安全与容量检查。

测试失败必须说明它阻断的是“代码合并、候选版本还是生产发布”。不得再用一个总 Gate 同时表达三种含义。

## 7. 历史切换回执

历史本机路径、业务数据统计和运行回执在私有资料中保留，不随源码发布。此文是 2026-07-28 的迁移设计记录，不定义当前开发顺序或部署状态；现行入口见[本地部署](../deployment/local-native-postgresql.md)、[项目状态](../项目.md)。已有 PostgreSQL 数据不得为复现旧迁移而重置或覆盖。
