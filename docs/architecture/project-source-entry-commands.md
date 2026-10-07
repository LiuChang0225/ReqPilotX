# 项目创建与来源导入命令

2026-10-06，源码开发候选。项目创建与 PDF/来源导入复用现有业务 API、数据库连接和对象存储，不建立第二份项目或文档台账。尚未发行、部署或通过真实员工宿主验收。

## 接口与持久结果

| 动作 | 命令字段 | 保存后查询 |
|---|---|---|
| `POST /api/projects` | JSON `command_id` | `GET /api/projects/creation-commands/{command_id}` |
| `POST /api/projects/{project_id}/sor-documents` | multipart `command_id`，与 `file` 同表单 | `GET /api/projects/{project_id}/sor-documents/import-commands/{command_id}` |

命令 ID 为 1–200 位 ASCII 字母、数字、`.`、`_`、`:`、`-`，首位为字母或数字。老客户端可不传，其独立提交仍各创建一份记录。新的共享客户端在提交前持久保存命令身份，Pair 的 JSON/multipart 代理原样转发，不依赖未转发的自定义 header。

数据库 `entry_command_receipts` 的主键为 `(kind, scope_id, command_id)`。项目创建在当前单 owner 服务中使用空 scope；导入以项目 ID 为 scope。请求 SHA-256 与原始结果快照绑定。项目名称/客户名称先去首尾空白；导入指纹绑定项目、原文件名、原字节 SHA-256、大小和媒体类型，不以同名/大小判断字节相同。

同命令同请求返回最初保存的记录，不因后续预览或业务状态变化更改重放结果。同命令不同请求返回 `409 entry_command_id_conflict`。业务记录与完整回执在同一事务内保存；唯一 insert 在 PostgreSQL 等待竞争者提交或回滚，在 SQLite 测试库串行写入。回执完成或业务插入失败会共同回滚，原命令可重试。

查询返回已提交结果；`404 entry_command_not_found` 仅说明查询时未见已提交回执，不能解释为另一请求必然失败或没有在途。网络中断、超时、取消传输、5xx 或提交确认丢失时保留原命令，先查询，再按同命令重放。服务路由本身不存在的普通 404 不允许客户端据此重放。原资源被删除后查询/重放返回 `410 entry_command_result_deleted`，不会借旧命令重新创建。

## 客户端恢复

共享 `createProjectDocumentClient` 管理在途请求和本地 pending intention。项目按规范化字段识别原请求；文件按真实字节 SHA-256 识别，包括重新选取的同一文件。已知保存成功后才移除 pending identity；无法先持久保存身份时，在发送业务写入前失败。pending 数据只含命令与字段/哈希，不保存原件字节。

完成旧查询时只清除仍属于该命令 ID 的 pending 记录，避免覆盖之后同 payload 的新动作；缺失或非字符串命令 ID 的损坏记录在写入前拒绝。上传取消在摘要开始前/完成后检查；在途传输取消仍查询结果，已提交则恢复成功，未知则保留命令并保持 `AbortError` 供队列识别取消。

Pair 使用稳定 `applicationId` 命名空间，服务端口改变不改变命令；独立 Web 使用浏览器 origin。页面重载会查询已有 pending command 并恢复项目/来源选择，未知结果保留供原请求重试。一项查询失败不会丢弃其他已找回结果。成功保存与随后列表刷新分开处理，刷新失败不会把已保存动作重新当成创建/导入失败。新动作在上一动作成功后获得新命令 ID，可有意建立同名项目或再次导入同字节文档。

## 原件补偿和权限边界

来源 object key 按项目、字节 SHA-256 与安全文件名共享。`stat` 未见对象或 `put` 成功不授予该请求独占删除权：其他请求可能已引用、正在引用该 key，或者本请求的事务已提交但确认丢失。因此导入失败不执行即时 object delete。上传失败可能留下孤立对象，后续只能通过覆盖已提交及在途引用的持久锁/引用 GC 清理；现有导入路径尚无此 GC，不把保留对象标为已清理。临时 staging 在 PostgreSQL 路径始终清理；SQLite 测试路径只在串行查询确认本次独立路径无引用后删除，不确定时保留。

显式移除 SOR 文档同样保留共享原始对象及 `stored_objects` metadata。原先“释放 metadata 后物理删除”的窗口可擦掉并发导入刚提交的原件引用，已由私有 PostgreSQL 确定性回归复现。文档绑定、review 文本、预览及派生缓存照常清理，旧命令返回 410；原始 bytes 的最终回收需要持久删除栅栏。知识文档及派生 source-asset 对象回收不在本轮入口原件验收范围。

现有 Pair authentication、managed/readonly 写白名单、固定 service origin 与禁止转发 Pair cookie/auth 的规则未放宽。查询导入回执先检查项目并使用项目 scope，不能从其他项目取同名命令的结果。当前仍是 source-only、单 owner 运行边界；scope/命令/Pair applicationId 都不是 trusted principal 或 tenant 授权。多租户隔离未实现或验收；引入 trusted principal 时需沿现有权限链补充服务端 actor/tenant namespace，不能只相信客户端传来的身份。

## 迁移、验证与接续

PostgreSQL 增量迁移为 `0038_entry_command_receipts.sql`；SQLite `init_db` 提供测试等价表。迁移不合并、删除或回填旧项目/文档，旧记录无法事后推断其创建命令。部署新服务并应用迁移后再使用对应新客户端；本轮未构建发行物或修改运行中数据库。

2026-10-06 UTC 本轮最终执行结果（97 项 PASS；不把重复运行累加）：

| 验证 | 真实结果 |
|---|---|
| 隔离后端 `test_entry_commands.py` | 17 PASS：HTTP JSON/multipart、重放/冲突、并发、回滚、删后重放、项目 scope、失去提交确认与原件保留 |
| 原上传边界 `test_document_ingestion_boundary.py` | 12 PASS：拒绝非法文件、上传限额、身份校验与临时文件清理 |
| `tests.test_entry_commands_postgres` | 6 PASS：持久唯一约束、同命令与冲突 payload 并发、真实 PDF preflight、事务回滚、共享 key 及失去回执恢复 |
| Vitest：`projectDocuments.test.ts`、`client.test.ts`、`ProjectManagementPanel.test.tsx`、`uploadQueue.test.ts` | 50 PASS：稳定命令、重选字节、失联/旧服务/冲突处理、重复事件、重载恢复及保存后刷新失败 |
| Pair `host.test.mjs` + `application-discovery.test.mjs` | 11 PASS：body/form ID 转发、authentication、managed/readonly 白名单与 credential/origin 边界 |
| Pair `entry-recovery-ui.test.mjs` | 1 PASS：当前源码、已有 React18/JSDOM、小型 API 模块在内存中构建；重复提交、创建/PDF 回复丢失、查询不可用、重载、服务端口变化及保存后列表刷新失败 |
| `tsc --noEmit`、受影响 Python Ruff、`git diff --check` | PASS |

后端运行：`PYTHONDONTWRITEBYTECODE=1 prototype/.venv/bin/python -B scripts/test_python.py backend --pattern test_entry_commands.py`，原边界以同入口 `--pattern test_document_ingestion_boundary.py` 执行。PostgreSQL 使用已有 Homebrew PostgreSQL 16 二进制、专用临时 Unix socket 实例和 UUID schema，执行 `PYTHONPATH=prototype REQPILOT_ENTRY_POSTGRES_URL='<专用本机测试连接>' prototype/.venv/bin/python -B -m unittest -v tests.test_entry_commands_postgres`；现有迁移全部应用并验证重用，实例、schema 和测试对象随后停止/删除，未连接业务数据库。

Web 命令：`node node_modules/vitest/vitest.mjs run --config vitest.config.ts packages/workbench-ui/src/shared/api/projectDocuments.test.ts packages/workbench-ui/src/shared/api/client.test.ts packages/workbench-ui/src/features/projects/ProjectManagementPanel.test.tsx packages/workbench-ui/src/features/documents/uploadQueue.test.ts`。Pair 以 `node --test` 运行表中相应文件，未构建共享 UI/Pair 发行物。

实际失败/修正：Pair 本机代理首次运行因默认沙箱禁止 `listen 127.0.0.1` 而有 3 项 FAIL；获准后重跑 11 项 PASS，无自动审批拒绝。新后端测试的兼容 import、前端 union 类型、源码 UI 测试对 TypeScript 7/异步摘要的适配，以及新增 Web 测试的按钮名称/spy 配置曾失败，均已修正并运行通过。源码 UI 回归实际发现了保存项目选择缺失，已修业务代码再验证，不以改断言掩盖缺陷。

NOT_RUN：真实 Pair 员工浏览器/已安装候选、完整付费模型任务、OSS/RDS 故障注入、多租户授权。失败上传对象的持久 GC 尚未实现；本轮以保留字节消除误删风险。老客户端无命令的历史重复记录未合并。磁盘开始约 255 MiB，临时 PostgreSQL 最低实测约 205 MiB，清理后约 244 MiB；无依赖安装、大型缓存或发行包。

工作从干净的 `9a8fdbd1d57b6c6449eb63c350ca4bb2ebd01546`、`codex/reqpilotx-v1-cloud-v2-20261001` 开始，修改保留在 canonical Workspace。未提交、推送、合并、部署，未安装依赖或调用付费模型。Library 交接 `libfile_084ff258660c8191909270c6a9135c23` 的 materialization 只返回云端路径，本机不存在且无传输 URL；58120 bytes 与预期 SHA-256 未核验，交接中的历史计划测试不作本轮 PASS。

接续：由主会话核对本轮 23 个源码/测试/说明路径后决定提交与发行验证；保持单 owner 边界。后续若清理孤立上传对象，先实现覆盖在途引用的持久删除栅栏，不能恢复当前已经删除的失败补偿。

## 2026-10-07 独立复核

重新核对 canonical Workspace：HEAD 仍为 `9a8fdbd1d57b6c6449eb63c350ca4bb2ebd01546`，分支未变。本次提交建议只含下面 23 个路径；并行维护 CLI 的 11 个未跟踪源码/说明/测试文件不纳入、不修改、不执行其数据操作。原范围在重启复核时无已知他人改动；07:21 UTC 复核结束时修改均留在工作区，未提交、推送或部署，后续获准同步单独核验。

服务端由只读子代理独立审查，客户端由主执行者复核。发现并先以失败回归复现后修正：

| 优先级 | 实际触发与修正 |
|---|---|
| P1 | 旧回执查询晚于新同 payload 动作返回，清除了新 pending ID；完成清理增加命令 ID 比较 |
| P1（既存） | 显式删除释放 metadata 后，新导入提交，随后物理删除擦掉新引用 bytes；入口删除保留原始对象/metadata，等待持久栅栏 |
| P2 | 摘要期间取消仍进入提交、在途取消被普通 Error 包装；摘要前后检查取消并保留 `AbortError` 和未知命令 |
| P2 | Pair 回执已清除后的普通重载先写空 document ID，丢失 PDF 选择；读取恢复快照并在列表加载期间保留选择，主动返回文档列表仍清空 |
| P2 | 本地记录缺 ID，正则将 `undefined` 转为字符串通过，最终发送无 ID 的 legacy POST；严格检查 ID 字符串类型 |

2026-10-07 UTC 实际重跑 **105 个唯一测试 PASS**，不累加重复运行：原 97 项、4 个新客户端回归、1 个新 PostgreSQL 删除竞态回归、3 个已有删除缓存回归。

| 验证 | 本次结果 |
|---|---|
| `test_entry_commands.py` / `test_document_ingestion_boundary.py` | 17 / 12 PASS，隔离 SQLite + ASGI |
| `tests.test_entry_commands_postgres` | 7 PASS，专用临时 PostgreSQL 16 Unix socket、UUID schema、全部 38 项迁移与重复应用；实例/schema/对象随后清理 |
| 原四个 Vitest 文件 | 54 PASS（19 + 21 + 12 + 2），含旧查询竞态、取消与损坏记录 |
| Pair Host/discovery | 11 PASS，当前本地源码；不引用旧远端 CI |
| Pair 源码 UI | 1 PASS，扩展普通重载和主动返回列表的断言；合成 transport、已有 React18/JSDOM |
| 原删除缓存回归 | 3 PASS：preview/snapshot、review 文本/result、purge 失败保留绑定 |
| `tsc --noEmit`、受影响 Python Ruff `--no-cache`、`git diff --check` | PASS |

3 个删除缓存回归为 `tests.test_unified_workspace.UnifiedWorkspaceTests.test_delete_document_purges_preview_and_snapshot_artifacts`、`tests.test_llm_requirement_review_api.LlmRequirementReviewApiTests.test_delete_document_purges_review_text_and_cached_result`、`tests.test_llm_requirement_review_api.LlmRequirementReviewApiTests.test_visual_snapshot_purge_failure_preserves_document_binding`，使用与现有离线入口相同的隔离环境和禁止网络的 audit hook。临时运行脚本不作为源码交付。

实际验证失败也保留：新竞态/取消/普通重载/损坏记录断言先 FAIL；新 PostgreSQL 删除夹具最初缺 review repository，补全夹具后复现真实 bytes 缺失；新慢查询 Mock 的泛型导致一次 typecheck FAIL，修正测试类型后 PASS。默认沙箱的 `initdb` 因共享内存 EPERM 失败，获准后仅在专用临时实例完成测试；无自动审批拒绝，没有换通道访问真实数据库。

复核结论：在当前单 owner/source-only 的项目创建、SOR 原件导入与结果恢复范围内，可建议将精确 23 文件提交到开发分支。当前未发现尚未修正的可证实 P1/P2；这是源码集成质量结论，不是员工宿主或发布验收。交接文件仍未成功 materialize，**26 个完整计划场景未逐项验收**。真实员工浏览器/已安装候选、OSS/RDS 故障注入、完整付费任务、多租户授权、多浏览器标签的原子命令获取均 NOT_RUN；localStorage 没有跨标签锁，不能声称多标签同时发起同一意图必然共用 ID。原始对象保留会增加存储占用，持久 GC 仍未实现。

磁盘复核开始 07:08:46 UTC：`df -k` 可用 17,000,000 KiB（16.21 GiB）；07:18:36 UTC 可用 16,960,000 KiB（16.17 GiB）。磁盘缓解来源未知，不能归因本任务；未安装依赖、拉大包、发行构建或操作真实数据库。

后续用户授权精确范围提交/推送到既存开发分支、保持 PR10 草稿并等待新提交 CI。已核原 origin 私有属性、远端分支/PR HEAD 均为复核基线；没有改 remote、权限、凭据或部署。23 文件暂存前逐文件调用安全规则，0 findings；仓库规定的 `repository_safety_audit.py --staged` 检查 2,622 blobs，PASS/0 findings，暂存 diffcheck PASS。可选全工作区模式对既存 `prototype/migrations/postgresql` 目录符号链接报 `IsADirectoryError`，未完成，不记 PASS；未修改该审计脚本或链接。实际索引模式读取 Git blob，不受该工作区读取错误影响。维护 11 文件哈希保留用于提交前后比对；GitHub 新 HEAD 的 CI 结果由 PR 同步记录，旧 HEAD CI 不作为本提交通过证据。

### 完整差异摘要及建议提交清单

服务端 7 文件与 1 项迁移提供命令验证、同事务唯一回执、冲突/删除状态、结果查询与原件保留；共享客户端 3 文件提供持久 ID、字节身份、在途合并/恢复与 HTTP 状态；Web/Pair 两个入口文件恢复选择并区分已保存与列表刷新；6 个测试文件覆盖数据库、API、共享客户端与宿主源码；4 个说明文件记录接口、迁移、删除语义与真实验证边界。合计 23 文件（16 个原跟踪修改、7 个新增），建议逐路径暂存，避免 `git add .` 混入维护任务。

```text
apps/server/README.md
apps/server/src/reqpilotx_server/adapters/persistence/entry_commands.py
apps/server/src/reqpilotx_server/api/routers/ingestion.py
apps/server/src/reqpilotx_server/api/routers/projects.py
apps/server/src/reqpilotx_server/application/ingestion.py
apps/server/src/reqpilotx_server/application/projects.py
apps/server/src/reqpilotx_server/application/projects_contracts.py
apps/server/src/reqpilotx_server/db.py
apps/server/src/reqpilotx_server/resources/migrations/postgresql/0038_entry_command_receipts.sql
docs/architecture/project-source-entry-commands.md
docs/deployment/local-native-postgresql.md
integrations/pair/README.md
integrations/pair/client/src/catalog.cjs
integrations/pair/tests/entry-recovery-ui.test.mjs
integrations/pair/tests/host.test.mjs
packages/workbench-ui/src/features/projects/ProjectManagementPanel.test.tsx
packages/workbench-ui/src/features/projects/ProjectManagementPanel.tsx
packages/workbench-ui/src/shared/api/entryCommands.ts
packages/workbench-ui/src/shared/api/projectDocuments.test.ts
packages/workbench-ui/src/shared/api/projectDocuments.ts
packages/workbench-ui/src/shared/api/uploadTransport.ts
prototype/tests/test_entry_commands.py
prototype/tests/test_entry_commands_postgres.py
```
