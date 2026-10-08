# ReqPilotX：既有 2GiB ECS 的受保护私有试运行

更新：2026-10-07。目标实例：北京现有 ECS（2 vCPU / 2GiB，Alibaba Cloud Linux 3.2104 U13.3 x86_64）。本页是可核验的发布与运行方案，不表示服务器已部署、公网已开放、模型链路已验收或客户资料已获准上传。

## 1. 发布边界与选型

首先只运行本机回环地址的私有 pilot。新建付费资源、升级规格、RDS、OSS、Redis、Qdrant、公共镜像仓库和域名购买均不属于本次方案。保留现有 FastAPI、共享 Python engine、React/Vite、PostgreSQL 和独立 durable worker，不改写框架。

Web 文档主入口是 `DocumentWorkSurface → SourceItemAgentLoop`：从原文件开始调用 `POST …/agent-structure/delivery/extraction-loop`；已有工作版的意见调用 `…/delivery/loop`。这两个 native provider-tool-loop 使用本发布的正式注册 engine/plugin tools，已由真实 PostgreSQL 队列和 WorkerRuntime 的 SDK MockTransport 合成闭环验证；模型网络响应仍为模拟，不代表真实付费 provider 已接通。SaaS 页面先读 `GET /api/runtime/agent-readiness` 与 `…/agent-workflow-readiness`，检查同一 provider/model 配置、文档处理授权、来源读取、数据库、对象存储和后台 worker；缺失、未知、不可用或读取失败时禁止新建/继续任务并显示原因，暂停已存在任务仍可用。配置就绪只表示离线配置检查，在线认证、模型权限和 provider 服务可用性需单独验收。

旧“已有 Map 补全原文条目”“处理当前正文/原表”、单条分类复核、独立拆分/分析及 Codex 原文基线重解析属于另一兼容/分窗口路径，可能依赖独立 `agent-kit/` 或本地 Codex 执行器，不等于上述正式 portable tools。首个 SaaS profile 不提供该 Kit，也未开放这些写路由；SaaS UI 明确禁用并提示使用文档 Agent 协作，不能自动切换执行器。单条意见入口可显式打开文档协作工作区，由用户核对后发送；既有本机条款草稿保留，不宣称已保存到服务器。LOCAL/TEST/Pair 原有兼容路径保留。独立 Kit 的资源、版本、许可及执行验证补齐后才能在后续候选开放。

2026-10-08 未提交兼容候选的百炼配置：`REQPILOT_AGENT_RUNTIME=provider-tool-loop`、`REQPILOT_AGENT_PROVIDER=aliyun`、`REQPILOT_ALIYUN_CHAT_MODEL=qwen3.8-max-0902`、`REQPILOT_AGENT_MODEL_ID=qwen3.8-max-0902`，沿既有北京连接 profile 配置 `REQPILOT_DASHSCOPE_BASE_URL`，凭据仍走现有私有配置渠道。`REQPILOT_AGENT_ALIYUN_ENABLE_THINKING=1` 默认显式开启思考及历史保留；一页合成 PDF 的非思考验收用 `0`，显式关闭 `enable_thinking` 与 `preserve_thinking`。任务开始仍要求已授权的 `REQPILOT_AGENT_CUSTOMER_DATA_ENABLED=1` 及原 readiness 条件，本轮未启用生产任务。按用户最新要求没有新增调用次数、累计预算或输出 token 上限，既有超时、取消和 160 步循环保护保留；这组配置不提供人民币硬上限。详见[思考兼容、恢复与未覆盖调用](../issues/provider-tool-loop-foundation.md#qwen38-max-thinking-compatibility-2026-10-08-uncommitted-candidate)。

首选最小原生 profile：官方系统仓库并行安装 Python 3.11 和 PostgreSQL，以独立 venv 和 systemd 运行 API / worker。不要替换系统 Python 3.6，不做全系统升级，不在 ECS 上安装 Node 或构建前端，也不在服务器编译 Python 或 ML 库。已通过官方 Workbench 只读核验：系统 systemd239；仓库提供 Python3.11 `3.11.13-7.0.1.al8`、pip `22.3.1-5.al8`，以及 PostgreSQL13 `13.23-3.0.1.al8`（最终执行前仍核对 exact package NVR）。官方 Alibaba Linux 文档明确 3.2104 支持并行 Python 3.11，建议使用版本后缀和 venv：[官方 Python 安装说明](https://www.alibabacloud.com/help/en/alinux/support/install-a-newer-python-3-on-alibaba-cloud-linux-3)。

PG13 upstream 已于 2025-11-13 结束支持，但 Alibaba 的 2026 release notes 明确为该发行包回补 CVE-2026-6478；这证明该补丁，不代表无所有漏洞或未来维护承诺。选择 vendor-maintained13 的私有 pilot 时先核验相关 advisory/package 匹配，不得把兼容性测试当安全维护保证：[PostgreSQL 生命周期](https://www.postgresql.org/support/versioning/)、[Alibaba 安全更新记录](https://help.aliyun.com/zh/alinux/product-overview/release-notes-for-alibaba-cloud-linux)。后续正式公共运行应明确支持期限和更新方案。

安装前先只读核对 `dnf list --showduplicates python3.11 python3.11-pip postgresql-server postgresql-contrib`，再锁定该机器实际提供的受支持 PostgreSQL 版本。不要根据文档中的示例版本安装不可用的包。PostgreSQL client/server 为必要组件，contrib 只有实际迁移需要时才装。本仓库现有迁移不要求扩展。

容器 fallback 的 file-secret bind mount 会保留 host uid/gid/mode，Compose 的 file-source uid/gid/mode 字段不会替用户重新赋权。invitation 文件要可被容器 UID10001 读取；database password 仅在已确认创建后置于 root0700 目录中，文件可使用 root0444，让获挂载的 PG/API/worker 读到它，其他 host 用户无法穿越父目录。不要假设容器内秘密天然加密或该权限等价于 Swarm。

容器 fallback 位于 `deploy/saas/compose.yaml` 和 Dockerfile；不是同时安装的第二套栈。采用它需要先确认 Docker daemon、容器网络和服务权限的精确范围，并在独立可用构建机制作完整 OCI 镜像。没有可用 Docker builder 时不能声称已构建或验证镜像。所有镜像需固定 digest / image ID，并经离线导入；禁止在 2GiB ECS 上构建。

## 2. 文件与资源预算

原生文件布局：

- `/opt/reqpilotx/releases/<revision>/`：两个 wheel、hash-locked 依赖 wheelhouse、预构建 frontend、发布 manifest、运维脚本，以及该发布的独立 venv；代码由 root 持有、服务只读
- `/opt/reqpilotx/current`：原子更新的已验证发布指针
- `/etc/reqpilotx/application.env`：明确配置，模板见 `deploy/saas/application.env.example`；root 持有，服务组只读，不含模型密钥
- `/etc/reqpilotx/secrets/invitation_code`：在用户控制的安全终端内写入，root:服务组 0640，父目录限制访问；不回显、不放在命令参数/聊天/Git/发布物中
- `/var/lib/reqpilotx/application/`：`reqpilotx` 无登录 shell 的服务用户所有，0700；对象、上传、派生工件和 scratch 均持久化
- `/var/lib/pgsql/data/`：仅本机 PostgreSQL cluster；不可写入应用目录
- `/var/backups/reqpilotx/`：root 0700，一致性冷备份；本机副本不能保护整台机器或磁盘故障

初始上限：API 单进程 512MiB / 0.6 CPU，worker 单进程 512MiB / 0.6 CPU，PG 预算 320MiB / 0.4 CPU。应用服务使用 systemd `MemoryMax`、`CPUQuota`、`TasksMax`；PG 使用 64MiB shared buffers、32 个连接和 2MiB work_mem。API/worker 默认连接池最多各 8 个连接，仍留迁移/运维余量。PG 配置上限不能代替实际内存观测，若安装 profile 的 systemd 支持，可另加 PG 的 `systemd/postgresql.service.d/reqpilotx-budget.conf` drop-in 后核验。

这些是 cgroup 上限，不是为进程预先保留的内存；合计约1344MiB，不是容量充分或峰值已测量的保证。当前小型 smoke 没有证明并行 login + worker/PDF 的无 OOM 能力；该并发验收在现有 ECS 完成前，明确保持低并发与 synthetic/private 范围。当前云端最初 54 包轻量依赖环境约 172MiB 磁盘，最终 lock 为 59 包、离线 binary wheelhouse 约 64MiB，baseline API import-only RSS 约 207MiB；实际安装 wheel、Python3.11.16 与 real PG17 的两账户/上传/重启 synthetic smoke 最大 RSS381600KiB（约373MiB），所以 API 上限调整为512MiB，避免384MiB仅余约11MiB。scrypt仍保持原强度，密码计算并发为1。不能把该小型 smoke 值当作复杂 PDF 或生产峰值。大 PDF / OCR / 并发模型任务必须另做容量验收。文件数、上传体积、数据增长和本地备份均需观察；剩余磁盘不足时停止新导入，不能自动删客户数据或扩大付费磁盘。日志限额和 retention 应在部署时核对，不把 session/token/原文写入 access log。

## 3. 身份、权限与私有网络

必须设置 `REQPILOT_SAAS_ENABLED=1`；部署 entrypoint 会在导入 API 前强制验证 `SaaSSettings`，拒绝 SQLite test mode、空邀请码和无效 Origin。该 profile 使用 `REQPILOT_ENV=local` + 显式 SaaS 开关，保留 PostgreSQL / local object store。现有 `REQPILOT_ENV=cloud` 的 OSS 要求没有被偷偷放宽。

`REQPILOT_SAAS_ORIGIN` 是一个精确 origin。私有 smoke 仅允许 literal loopback HTTP；公共部署必须 HTTPS。正式会话使用服务端生成的不可猜测 opaque token，数据库只存 hash，并使用 Secure / HttpOnly / SameSite=Strict cookie、有限过期时间和 server-bound CSRF。当前设计不需要另造 cookie signing key。bootstrap 邀请码仅初始化一次，hash-only 入库、24 小时到期、首次注册在同一事务消耗；重启不延长或恢复。额外邀请由本机 `reqpilotx-saas-invite --output <new-private-file> --seconds 86400` 生成0600文件，CLI不回显token。邀请码只限制注册入口，不等于登录或数据授权。

应使用独立可信的浏览器 profile：account namespace 能防止不同账号的 UI 草稿混入，但退出仍保留可恢复草稿，不能隔离同 profile 的设备持有者或扩展。

Host / Origin allowlist 是补充边界，不能替代身份认证。授权必须覆盖 project / document / source / raw object / job / export / workspace routes，worker 恢复 job 时使用 server-trusted owner context，不能信任请求中的 account/tenant ID。遗留未鉴权 API、全局 runtime provider setup/probe/shutdown 等管理入口不能通过另一条路径暴露。

原生 PostgreSQL 使用 Unix socket + peer authentication，OS 服务用户和 PG role 都为 `reqpilotx`。DSN 示例为 `postgresql:///reqpilotx?host=/var/run/postgresql`，无需在应用配置中保存数据库密码。`listen_addresses=''`，HBA 仅允许 postgres 运维身份和 reqpilotx 对自己的数据库建立 local peer 连接。新建空 cluster 可参考 `pg_hba.conf.example`；有现存业务 cluster 时禁止直接替换配置。

API 只监听 `127.0.0.1:8000`，worker 不开端口。容器 fallback 的唯一 port mapping 也是 `127.0.0.1:8000:8000`；DB 网络 `internal:true`，无 ports mapping。即使用户允许安装，也不要自行打开 80/443、修改安全组、防火墙、SSH、证书信任或持久凭据。

## 4. 可复现发布与迁移

`requirements-runtime.in` 选取现有直接依赖；`requirements-runtime.lock` 含完整传递版本和 hashes，目标是 Python 3.11 / Linux x86_64 glibc 2.28。轻量 profile 不安装 torch、sentence-transformers、Haystack、向量服务、桌面包装或 OSS SDK；这些功能不在本次容量承诺内。不要直接 `pip install apps/server` 后再发现它拉入重型可选适配器。服务 metadata 和 optional extras 应随主实现核对。

离线 release 的制作步骤由 `deploy/saas/build-release.sh <revision> <output-directory>` 描述：只在构建机运行 npm ci / typecheck / Vite build，生成 engine/server wheel，下载针对 CPython 3.11 的二进制 dependency wheelhouse，按显式 allowlist 打包。产物不含客户文件、运行数据、.env、模型 key 或桌面认证状态。`release.json` 同时保存每个工件 hash、size 和源代码 snapshot hash。revision label 是发布标签，不自动宣称是 Git commit。

服务器安装前通过已批准的目标/渠道传入工件，核对外部交付 checksum，然后运行 `verify-release.py <release-root>`。只读核验过的目录才能安装 venv：

```sh
python3.11 -m venv /opt/reqpilotx/releases/<revision>/venv
<release>/venv/bin/python -m pip install --no-index --find-links=<release>/wheelhouse \
  --require-hashes -r <release>/deploy/requirements-runtime.lock
<release>/venv/bin/python -m pip install --no-index --no-deps <release>/wheels/*.whl
```

示例命令是维护操作说明，不是未确认授权的自动脚本。先前发布需保留。安装服务后必须先停写入、备份 DB+objects，再运行 `reqpilotx-migrate.service`，通过后运行 `entrypoint.py preflight`，最后才启动 API/worker。版本化迁移核对历史 checksum 并用 PostgreSQL advisory transaction lock；不要改写已应用 migration 或用旧 SQLite 覆盖新 PG。

不要仅凭 systemd active 或 `/api/healthz` 的 sanitized DB healthcheck 认定业务 ready。迁移失败、配置失败、缺失 auth 表、owner/context 失败、启动失败都必须停止发布；不打开公网作为调试绕路。

## 5. 备份、重启与回滚

systemd 服务使用 `Restart=on-failure`、5 秒 backoff，writer 停机允许 180 秒完成受控退出。worker durable lease / job state 位于 PG，发布文件和对象位于持久 state；重启后需验证既有 job 身份、owner context 和幂等结果，不能只检查 worker 进程存在。

`backup-native.sh --approved-maintenance` 是本机一致性冷备份：记录当前 active 服务，锁定备份操作，停止 API/worker，在同一静止窗口生成 pg_dump custom format 和 application tar，记录当前 release manifest 和 checksum，再恢复原来运行的服务。失败的部分备份不标记为完成。备份不复制邀请码和 provider secret，不自动删除旧备份，不上传其他云服务。第一次正式接受持久写入前，必须在另一个受控空数据库和隔离对象目录中完成恢复演练。

前向兼容且无新写入时可将 `current` 指回已保留的前一版本再启动服务。数据库迁移和实际写入发生后，代码 rollback 不能倒退 schema 或恢复数据；需验证兼容后切版本，或在停写入后恢复同一时点 DB+objects。恢复、删除数据和更改现存权限需独立确认。

私有 loopback 上线不会产生可供手机直接打开的公网 URL，不能把这一阶段称作完整 public SaaS。后续如需安全访问，可由用户选择已有 SSH 安全转发，或 Alibaba 官方 [Session Manager CLI port-forward](https://help.aliyun.com/zh/ecs/user-guide/perform-port-forwarding-by-using-ali-instance-cli)；CLI 安装、登录和权限范围应另行明确，不复制浏览器/Mac 凭据，不自行配置 proxy/tunnel 绕过既定访问边界。等待访问方案不阻止本轮在服务器 loopback 验证 health、worker、持久化和 synthetic export。

## 6. 公网与 HTTPS 是独立决策

用户目前没有域名。Let’s Encrypt 的 IP 证书于 2026-01-15 已正式开放：IPv4/IPv6、shortlived profile，160 小时有效期。Certbot >=5.4 支持 IP webroot 申请；需自动 renewal，并在每次成功更新后让 TLS 服务 reload。IP 证书不要求购买域名，但仍要求该公网 IP 的控制权验证以及 HTTP-01 或 TLS-ALPN-01 可达：[官方 GA](https://letsencrypt.org/2026/01/15/6day-and-ip-general-availability)、[官方 Certbot 用法](https://letsencrypt.org/2026/03/11/shorter-certs-certbot)。

这是后续可选技术路线，不是本轮开放服务的授权。Certbot staging 证书不受浏览器信任，不可要求绕过警告或将其当作正式可访问 URL。正式申请必须先向用户说明将向 Let’s Encrypt 提交的 IP/域名、可选邮箱、用途，并确认接受适用 Subscriber Agreement。当前公开协议为 [2026-07-06 v1.8](https://letsencrypt.org/documents/LE-SA-v1.8-July-06-2026.pdf)；申请当天仍需重新核对协议版本。

北京属于中国内地。Alibaba 官方网站建设说明写道：“Per MIIT regulations, any website hosted on a mainland China server must have a valid ICP filing before going live.” [官方原文](https://help.aliyun.com/en/dws/getting-started/the-whole-process-of-website-building/)。本页不能确认该用户已有备案或是否满足某个特定私有服务例外。IP 证书、邀请码、登录验证都不替代备案；裸 IP 不是绕过依据。公共发布前须核验备案/服务适用依据和账号控制台实际条件；如有法律适用疑问，由用户或具备资质的顾问确认。

公共阶段如获准，可在现有主机加一个最小 Nginx TLS reverse proxy，仍仅向 loopback API 转发，不另开放 API/PG/worker。80 仅用于 ACME challenge 和 HTTPS redirect，443 提供应用。需明确允许安全组/防火墙 exact 80/443 入站、TLS 客户端安装、CA 条款/注册、续期与 reload 服务，指定合法可信的 public origin；Nginx 不提供绕过 auth 的对象、导出或 API 路径。没有这些条件时继续私有 pilot。

公开/商业 SaaS 还需单独核对现有依赖许可证：PyMuPDF / PyMuPDF4LLM 官方说明为 AGPL 或 Artifex 商业许可，许可选择可能涉及 source 提供义务或商业费用；本轮未确认已有商业许可或整个产品许可方案，不自动采购、不修改项目许可、不公开源代码：[官方许可](https://pymupdf.io/licensing)、[官方 FAQ](https://pymupdf.readthedocs.io/en/latest/faq/index.html)。这不是对具体产品法律义务的最终结论；正式对外前由用户核验许可适用范围。

## 7. 精确授权与验收清单

执行者在现有实例安装前需向协调者确认：

1. 源码/发布工件交付到该既有 ECS 的具体渠道和范围已获准；不得读取或搬运 Mac 上 Azure/Bailian/SSH/Codex/Pair 登录状态
2. 实际可安装的 Python 3.11 / PG client/server package 版本和官方来源已核验；只安装必要包，不新建付费资源、不替换系统 Python
3. 无登录 shell 的 reqpilotx 服务用户、PG peer role、私有文件布局、systemd 常驻服务和备份维护窗口属于授权范围
4. 邀请码由用户在安全终端内输入或生成；代理不回显/读取/复制它；新凭据与持久访问仍遵循所需确认方式
5. 没有公网开放、没有证书注册、没有模型 key/transmission、没有客户文档或真实模型费用，除非另有精确确认

私有 pilot 最低验收：两个独立 account 的注册/登录/退出/过期、无效 invite、cookie flags、CSRF 与 Origin/Host 攻击、匿名拒绝、互相不可读写项目/对象/jobs/exports、owner-bound durable worker 重启、PG+objects 持久化、迁移 checksum、备份完整性与隔离恢复；同时核验 `ss -lntp` 没有 0.0.0.0 / public API 或 5432。记录包/服务版本、exact candidate identity 和实测 RSS/CPU/剩余磁盘。

聚焦检查、import smoke、shell syntax 或云端测试通过都不等于以上服务器验收已完成。若真实 provider 权限/额度/密钥、上传目的地、客户资料授权未建立，报告为未验收，不把无模型的功能当作 AI SaaS 已完成。
