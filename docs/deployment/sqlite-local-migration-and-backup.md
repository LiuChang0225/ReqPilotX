# 本地 PostgreSQL 副本迁移到 SQLite 与备份恢复

更新时间：2026-10-31

本文只描述本地迁移/备份交付的独立工具边界。它不是“整体迁移完成”声明，也不替代 RX03/RX05 的业务资格与 RX07 的工作台/Excel 产品验证。源 PostgreSQL 始终保留；工具不会启动、修改或删除源库、源对象或真实用户数据以外的任何内容。

## 1. 支持边界

- 源端：已应用本仓库全部 PostgreSQL migration（`0001`–`0039`）的显式项目子集。导出使用 `REPEATABLE READ` 只读事务；导出前校验源库 migration 版本、名称和 SHA-256 与当前包完全一致。未知 migration、未知表、未知列或凭据类列直接拒绝。
- 目标端：显式命名的新本地 SQLite 应用根。不覆盖已有目录；不自动选择源项目、目标目录或激活版本。目标不需要 PostgreSQL 进程或 `psycopg`，但仍使用已安装的 ReqPilotX server 包、SQLite 和文件对象；导入、校验、备份和恢复属于本地 edition 路径。
- 源对象：通过显式 `--object-root` 读取 `stored_objects`/JSON 中引用的对象；逐文件校验存在性、大小和 SHA-256，并将原始字节复制到导出包。相对对象路径逐组件使用 `O_NOFOLLOW`/`dir_fd` 打开，拒绝任何 symlink/junction 组件逃逸；读取前后核对文件身份，复制后复核目标字节。缺对象、路径逃逸、对象变化或 hash 不一致会失败。无法提供安全打开保证的平台明确失败，不回退到普通路径读取。
- 不导出：SaaS 账号/会话/邀请等凭据表、运行时 worker/job、暂存 page/asset、提取结果临时对象、全局 provider run 审计和旧 cutover 回执。原因写入 manifest 的 `excluded_tables`，不会静默丢弃。
- 项目所有权：`saas_project_owners` 的 project→account 关系不含凭据，单独保留在导出包和目标根的 `local-migration/project-ownership.json`；本地 SQLite 没有 owner 表，因此存在关系时 `activation_ready=false`（`SOURCE_PROJECT_OWNERSHIP_RETAINED_AS_EVIDENCE`），绝不把 owner 改写成当前本地用户。
- 仅导出保留：`saas_project_owners`、Agent Structure/Activity/table materialization 与 PostgreSQL source-asset GC 清单。它们保留在导出包中可恢复，但当前本地 SQLite 没有对应表；存在非空行时完成回执 `activation_ready=false`。
- 活动分析任务不会被续跑：`PENDING`/`RUNNING`/`CANCEL_REQUESTED` 任务在导入时显式标为 `FAILED`，错误信息包含 `Interrupted by local migration`；目标校验还要求不存在活动任务。原始状态和对象 ID 记录在回执的 `active_jobs_quarantined`。
- 路径：本地 SQLite 现有代码要求 `sor_documents.file_path` 等为应用根内绝对路径，因此导入时把源 `storage_key` 映射为目标根下 `objects/<storage_key>`；`stored_objects.storage_key`、Requirement Tree key、交付 `export_key` 等仍保留源 key。备份恢复到新根时会重写这些本地绝对路径。

## 2. 命令

从仓库根运行，使用能导入本 checkout 的 Python。`--database-url-env` 只读取环境变量，不把数据库 URL 写入日志或 manifest。

```bash
export REQPILOT_DATABASE_URL='postgresql://.../reqpilotx'
PYTHONPATH="$PWD/apps/server/src:$PWD/packages/engine/src:$PWD/prototype" \
  python -m reqpilotx_server.tools.local_migration inventory \
  --project-id PROJECT_ID \
  --object-root /absolute/source/objects \
  --output /absolute/inventory.json
# inventory --output is new-only: existing files, source-object-root overlap and races are refused.

PYTHONPATH="$PWD/apps/server/src:$PWD/packages/engine/src:$PWD/prototype" \
  python -m reqpilotx_server.tools.local_migration export \
  --project-id PROJECT_ID \
  --object-root /absolute/source/objects \
  --output /absolute/new-export \
  --knowledge-map /absolute/knowledge-map.json

python -m reqpilotx_server.tools.local_migration validate-export --export /absolute/new-export
python -m reqpilotx_server.tools.local_migration import \
  --export /absolute/new-export --target-root /absolute/new-local-root
python -m reqpilotx_server.tools.local_migration verify-target --target-root /absolute/new-local-root
```

备份与恢复：

```bash
python -m reqpilotx_server.tools.local_backup backup \
  --root /absolute/local-root --output /absolute/new-backup
python -m reqpilotx_server.tools.local_backup verify --backup /absolute/new-backup
python -m reqpilotx_server.tools.local_backup restore \
  --backup /absolute/new-backup --target-root /absolute/fresh-root
```

可选入口（尚未修改 `pyproject.toml`，由父任务决定是否集成）：

```toml
[project.scripts]
reqpilotx-local-migrate = "reqpilotx_server.tools.local_migration:main"
reqpilotx-local-backup = "reqpilotx_server.tools.local_backup:main"
```

## 3. 导出格式

导出目录是新的、不可覆盖的版本化包：

```text
manifest.json
tables/<table>.jsonl
objects/<storage_key>
knowledge-mapping.json        # 提供时
```

`manifest.json` 的 `format` 为 `reqpilotx-local-migration-export`、`format_version` 为 `1`。每张表记录列、主键、行数、JSONL 文件大小/SHA-256、排序行摘要和主键摘要；每个对象记录 storage key、包内路径、大小和 SHA-256；源 schema 记录完整 migration 版本列表和指纹。`validate-export` 会重新计算这些值并拒绝任何变化。表中不写客户正文的额外报告；JSONL 本身包含选中的业务行。可空/默认引用（例如空 `requirement_tree_object_key`、空 `payload_storage_key`）保留为空值而不是被当成路径；JSONB/JSON 字符串中显式 `storage_key`/`object_key`/`export_key` 引用无论已解码为 dict/list 还是仍为字符串，都使用同一 walker 收集。

### 3.1 表兼容性

导入：`projects`、`sor_documents`、`knowledge_documents`、`knowledge_document_syncs`、`requirement_sets`、`requirements`、`assessments`、`evidences`、`assessment_runs`、`ai_analysis_jobs`/targets/node attempts/events、`review_logs`、`export_jobs`、`batch_decision_operations`、`parsing_risk_acknowledgements`、表资产、entry classifications/receipts、`entry_command_receipts`、`table_projection_revisions`、source capture/visual/reading、`source_item_manual_decision_versions`、`source_item_delivery_versions`、`source_ir_snapshots`、LLM review、atomization/refinement/enrichment publication、matrix node states、impact receipts、`stored_objects`、`source_asset_managed_objects`。

列映射：`storage_key -> file_path`（source/knowledge/export）与 `payload_storage_key -> payload_path`（knowledge sync）。`ai_analysis_jobs.runtime_job_id` 和 PostgreSQL `_row_order` 是运行/origin 身份，导出时省略；其他未知列直接使导入失败。

仅导出保留：`saas_project_owners`、`source_asset_references` 与全部 `agent_structure_*`/`agent_table_materialization*`。不导出且不重放：运行 job/worker、暂存引用、提取结果临时对象、全局 provider 审计、SaaS 凭据、旧 cutover 回执。

## 4. 知识映射与激活准备

映射输入必须是 `reqpilotx-knowledge-mapping/v1`：

```json
{
  "schema_version": "reqpilotx-knowledge-mapping/v1",
  "mappings": [
    {
      "source_kind": "knowledge_document",
      "source_id": "SOURCE_DOC_ID",
      "status": "RESOLVED",
      "target_kind": "pair_document",
      "target_id": "EXPLICIT_TARGET_ID",
      "target_revision": "REVISION"
    }
  ]
}
```

- `status` 仅允许 `RESOLVED`、`UNRESOLVED`、`UNAVAILABLE`。`RESOLVED` 必须有目标 kind/id；未解析不得填写目标 id。
- 工具不会生成 PAIR ID，也不会猜测缺失映射。没有映射时所有导出的知识文档被记录为未解析；`activation_ready=false`，阻断码 `KNOWLEDGE_MAPPING_MISSING`。
- 映射缺失、未解析、目标 ID 缺失或映射到不在导出中的源时，分别报告 `KNOWLEDGE_MAPPING_INCOMPLETE`、`KNOWLEDGE_MAPPING_UNRESOLVED`、`KNOWLEDGE_MAPPING_UNMATCHED_SOURCE`。
- 即使阻断激活，知识原文件、`knowledge_documents` 元数据、source references 和对象 bytes 仍保留在目标根与导出包中，可恢复。
- 导入不自动激活任何版本，也不创建第二套知识库；它把提供的映射文件原样保留在目标根 `local-migration/knowledge-mapping.json`，在回执中记录完整 entries 和 SHA-256，并让 `verify-target`、备份和恢复继续校验该文件。映射缺失、条目改变或 digest 不匹配会拒绝通过，而不是只保留计数摘要。

## 5. 完成回执与校验

成功导入在目标根写入 `local-migration/receipt.json`、`local-migration/knowledge-mapping.json`（提供时）和 `local-migration/project-ownership.json`（存在所有权关系时）。回执记录：

- 导出 ID、manifest SHA-256、源 migration 身份、目标 schema 指纹；
- 每张导入表的行数、全行摘要、主键摘要和主键列；
- 对象数量与 SHA-256 摘要、对象索引；
- 活动任务隔离记录；
- 知识映射的完整版本化 entries、文件 SHA-256 和报告，以及 `activation_ready`/`activation_blockers`；
- 项目所有权原始 project→account 关系、文件 SHA-256 和 `/` 或显式激活阻断；
- 实际执行的完整性、外键、引用、人工意见、交付版本和 XLSX bytes 校验结果。

`verify-target` 重新校验 SQLite `integrity_check`、`foreign_key_check`、表摘要/主键、所有 `stored_objects` 与文档/知识/导出/Requirement Tree/交付对象 bytes、人工意见 artifact 的 hash/版本/source 绑定、交付 artifact 与 XLSX 工作簿 bytes，以及不存在活动分析任务；同时校验目标根内知识映射文件与回执中的完整 entries/digest 一致、所有权关系文件与回执 digest/rows 一致。映射缺失、改变或损坏，或所有权证据缺失/改变，都会拒绝通过。若目标没有回执则拒绝通过。

导入失败时不会发布最终目标根；失败 staging 保留为同父目录下的 `.<target>.local-migration-*.tmp`，内含 `.LOCAL-MIGRATION-INCOMPLETE`。成功前不会改名成目标根，因此不会让应用误选半成品。已有目标根、已有导出目录和已有备份目录一律拒绝，不覆盖。

## 6. 备份、恢复与回滚边界

`local_backup` 使用 SQLite online backup API（不是复制 live DB），在应用根锁内取得一致 `app.db` 快照，并复制其他文件、记录逐文件大小/SHA-256 和数据库逻辑摘要。备份 manifest 契约是 `reqpilotx-local-storage-backup/v1`。`verify` 校验每个文件、SQLite 完整性、逻辑摘要、数据库引用的对象 bytes，以及 `local-migration/` 中回执、知识映射和所有权证据的 digest；缺失或改变的映射/所有权文件不会进入备份。`restore` 只创建新的目标根，并把 `file_path`/`payload_path` 从原根重定位到新根，再刷新恢复后的回执表摘要；映射 entries 和所有权关系原样保留。

回滚边界：

- **开放新写入前**：可以停止应用，使用备份恢复到一个新根并切回旧代码；这是可验证的恢复路径。不要直接覆盖现有根。
- **开放新写入后**：旧备份已经落后，恢复到旧备份会丢失新写入。此时只能恢复同一时点的备份或 fixed-forward；工具不提供“无损降级”命令，也不声称旧快照可无损回滚。
- **卸载**：卸载代码不删除应用根、`app.db`、`objects/`、`local-migration/` 回执或备份。数据保留由部署方负责。
- 工具没有用户数据删除、对象清理或磁盘清理命令。

## 7. 已执行与未执行验证

本轮已执行聚焦合成测试 `prototype/tests/test_local_migration_and_backup.py`（16 项），覆盖导出/校验/导入/回执、显式项目隔离、源端 identity 列省略、对象 bytes/hash、嵌套 symlink 逃逸拒绝、空可选引用、已解码/字符串 JSONB 引用、所有权证据、知识映射 entries、人工意见、交付 XLSX、活动任务隔离、未知 schema/表/对象、缺失知识映射、导出-only 阻断、失败 staging、重复导入、inventory 新写/竞态安全、真实 public CLI 全链、SQLite online backup、WAL 内容、tamper 拒绝、恢复重定位和回执复核。

真实 PostgreSQL 合成演练（2026-10-31，PostgreSQL 16.14 Homebrew，隔离 TCP 回环临时集群）已执行：使用打包 PostgreSQL migrations `0001`–`0039`、有效合成 PDF/XLSX、显式选择/未选择项目、JSONB 对象引用、空可选引用、原 owner/knowledge mapping 证据和活动任务隔离，通过 public CLI 完成 inventory -> export -> validate-export -> import -> verify-target -> backup -> verify -> restore -> verify-target。源逻辑摘要前后一致；目标在停止 PostgreSQL 且屏蔽 `psycopg` 导入的环境下完成导入、校验、备份和恢复。该演练使用合成数据，不是客户数据迁移，也不等于 RX06/RX07 全部完成。

未执行：

- 真实客户/原库数据迁移或原 PostgreSQL 服务启动/停止：本轮仅使用一次性合成集群。
- Windows 上的安全对象打开/引用校验未取得 `O_NOFOLLOW`/`dir_fd` 保证时明确失败；本轮只在 macOS 合成路径验证。
- 原生工作台/真实 Excel 产品验收：属于 RX07。
- RX03/RX05 的修订/证据业务最终资格；本工具只提供窄迁移/恢复机制。

## 8. 剩余父接口/资格需求

- 若要把两个 `python -m` 入口暴露为安装 scripts，父任务需在 `apps/server/pyproject.toml` 增加第 2 节给出的 `[project.scripts]` 两项；本切片未修改 package exposure。
- RX07 需在导入后的新根上验证工作台打开、项目/文档引用、人工意见显示、冻结交付 workbook 下载和 Excel 内容。
- RX03/RX05 需确认下游知识绑定与修订/证据语义；在此之前 `activation_ready` 的未解析映射必须继续作为阻断，而不是被 UI 当作已完成。
- Agent Structure/table materialization 等仅导出表需要后续切片决定本地存储或继续保留在导出包；当前不回放、不伪造。
