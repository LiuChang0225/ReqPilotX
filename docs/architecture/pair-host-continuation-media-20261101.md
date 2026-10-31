# PAIR Host 继续上下文与 adopted 原件媒体

更新：2026-11-01。本文只记录 Host 适配层当前实际行为，不声明真实模型、原生 UI、客户数据或业务语义验收。

## 继续任务

`TaskLauncher.control(action=continue)` 在发送任何提示前，从该任务已绑定的公开应用路由读取工作台状态：

`/api/projects/{projectId}/sor-documents/{documentId}/agent-structure/delivery/plugin-workbench/{work_ref}`

Host 只接受同时满足以下条件的响应：schema 为 `reqpilot-plugin-workbench/v1`，`session_id`/`work_ref` 等于冻结的 `prepared.work_ref`，`project_id`、`document_id`、`source_sha256` 与 launch record 一致；当前 `revision_id` 和每条反馈的 `expected_revision_id` 必须属于该工作台的已保存 `versions`。不匹配、404、传输失败、无法解析或超限都返回明确错误，不能解释为“没有反馈”。无成果的工作台可以合法地返回 `revision_id=null` 和空反馈；这种状态与路由缺失、身份不匹配严格区分。

待处理反馈是 `feedback` 中尚未出现在 `feedback_resolutions` 的条目。Host 将反馈作为**版本化只读数据**写入同一原生 Session 的持久用户提示：

- 保留原任务目标和已正确、未受影响的成果；
- 要求继续原固定工作流中所有仍未完成、原本要求的阶段，直到可核对交付，而不是只修反馈；
- 反馈不是原文、不是工程师响应、不是审批，也不产生 COMPLIANT 或 Map 确认；
- 要求 Map 确认时，提示明确说明尚未确认/已确认及确认摘要，不把继续指令当作确认；只有 `confirmed_map_sha256` 与当前 `stages.MAP` 的 `artifact_sha256` 完全一致且 owner 未报告 `map_confirmation_required=true` 时才显示已确认，旧 Map 保留的确认哈希一律按待确认处理；
- 空列表如实报告 `total=0,pending=0`，并明确禁止用 `delivery_read.responses=[]` 或 `source_read(view=feedback)` 的拒绝推断“没有反馈”；大量列表在固定预算内展开，未展开项保留 `feedback_id`、revision 和目标，禁止声称已处理。

提示文本与反馈快照在首次派发前写入该 control record。同一 control 的同一 `requestId` 重试必须复用同一文本；若 Session journal 已记录该 `requestId`，直接以日志确认，不再读取反馈或重发提示。

## Adopted 原件/图片媒体

任务证据 MCP 新增两个最小工具：

- `knowledge_adopted_source(sourceId)`
- `knowledge_adopted_image(imageId)`

它们只接受任务绑定中的 source/image 标识，不接受任意路径。Host 复用既有 `adoptedSource`/`adoptedImage` 的冻结 library/version、caller 当前性和字节校验；返回：

- Host 私有 `dataRoot/task-media/{commandId}/` 下的只读缓存文件绝对路径；
- 安全文件名、MIME、实际字节 SHA-256、字节数；
- library/version 与 source/image 身份、availability、locator；
- source 的 `contentRevision` 是其实际二进制文件哈希；image 的记录表 `contentRevision` 单独标为 `recordContentRevision`，不与图片字节哈希比较。
- image 的 metadata 读取错误不会被吞掉：只有 owner 明确返回 `source-unavailable`/`not-found` 时才标记 `metadata=UNAVAILABLE`，版本、绑定、权限或传输错误继续失败；写回执和返回前再次核对冻结绑定。

`knowledge_adopted_read(kind=source)` 在只有原件、没有 normalized text 时继续返回 `ADOPTED_SOURCE_CONTENT_UNAVAILABLE`，但提示明确指向 `knowledge_adopted_source`。metadata-only 或不存在的原件仍拒绝，不读取被排除字节；approved derived 与 original 保持各自 availability 和哈希。

媒体读取不写入文本事实账本、不改 HMAC/attestation、不生成 COMPLIANT 或受控项目记录。审计回执单独保存在 `dataRoot/knowledge-media/{commandId}.receipts.json`，工具结果照常进入模型 Session 日志。读取 linked original 不提升 claim 的权威。

## 当前限制

- 不改变任务冻结包、TaskService 分析/交付路径、UI 或 Python 核心。
- 反馈读取只覆盖公开 Workbench 反馈路由；engineer responses 是另一条路由，不混入反馈数据。
- 媒体缓存只按单次 source/image 物化，不复制整个知识库；缓存清理/配额不在本切片。
- 未执行真实付费模型、真实员工 UI、客户数据或业务符合性验收。
