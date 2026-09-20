# ReqPilotX 数据与 API 现役索引

更新：2026-09-12。保留原文件名以维持文档入口；不再复制过时的全字段清单与固定路由数量。
当前接口由代码、生成合同和运行进程的 OpenAPI 定义。旧 v1 过程/枚举可从 Git 历史与清理前快照恢复。

## 接口真值

- `prototype/app/api/routers/`：HTTP 路由与参数映射。
- `prototype/app/application/`：用例、请求合同、版本和错误语义。
- `prototype/app/domain/`、`prototype/app/ports/`：来源、业务对象、仓储和外部能力合同。
- `prototype/frontend/src/shared/api/contracts.ts`：当前前端公共读模型。
- `agent-kit/schemas/`：已登记的 Agent wire/final schemas，不代表每种工作流都已通过业务验收。

运行本地应用后，可只读查看 [OpenAPI](http://127.0.0.1:8000/openapi.json)。
接口扩展时更新所属合同与测试，不在多个文档中手工复制所有字段。

## 知识文档原件读取

`GET /api/projects/{project_id}/knowledge-documents/{document_id}/file` 返回该项目已登记知识文档的原始文件，与检索同步/分析状态无关。可选 `expected_source_sha256` 查询参数锁定当前查看的来源；不接受文件系统路径或 object key 参数。

先验证项目和文档归属，再经既有 source store 读取并校验原始字节 SHA。成功为文件响应：保留登记媒体类型和原文件名，PDF 使用 `Content-Disposition: inline`，其他格式为 `attachment`；返回 `X-ReqPilot-Source-SHA256`、相同来源摘要的 `ETag` 与 `Cache-Control: no-store`。响应与错误不暴露本地路径。

项目/文档不匹配或原件不存在返回404；来源指纹缺失、变化或字节校验失败返回409；查询指纹格式错误返回422；存储暂不可用返回503。该只读入口不重新上传、改版、重建检索或运行模型，不能用它把知识来源升级为客户需求或符合性结论。

## 独立插件工作台承接

新增来源/版本绑定的`plugin-workbench`端点，承接PDF Map、原文条目、图像释读、拆分、结构确认与反馈，导出同一Matrix快照。它不创建模型任务、不覆盖应用工作版。端点、调用、并发/历史与当前限制统一见[接入合同](需求提取/Plugin工作台接入与反馈闭环.md#数据与接口)，不在此复制字段。

文档内任务发现新增`GET .../agent-structure/delivery/plugin-workbench/sessions`。返回真实session摘要及独立`unavailable`，`selection_policy=EXPLICIT_WORK_SELECTION`、`current_session_id=null`；不把latest、更新时间或最高vN视为当前任务。空任务可列出，来源变更和损坏索引不替换为另一份结果。具体目录/成果owner关系见[任务工作区合同](需求提取/任务工作区与接续工具.md)。前端消费者单独验证。

原生`extraction-loop/{task_id}/stages/{stage}`读取追加可选`artifact_sha256`、`stage_version_id`查询；不传时只读配套current。任务摘要另列`retained_stages`，阶段元数据区分current/pairing_status/retention_reason，历史读取仍不声称人工接受。`path`是只读阶段快照，`producer_path`指向原编制目录，两者不能互换。

## 业务身份与工件

| 对象 | 权威与边界 |
|---|---|
| 原始来源 / SourceIR / TableIR | 原始字节与来源定位；模型和浏览器不覆写 |
| 原文结构 | 来源顺序与直接内容/逻辑表/附件归属；不是工程需求派生关系 |
| SourceItem | 原文条目及分类，可为需求或保留内容，绑定来源和稳定身份 |
| SingularRequirement | 用户明确要求后的派生单一需求，原文父项保留 |
| Working revision | 一个完整可验证的工作结果，绑定前驱/范围与证据 |
| Requirement Set / Matrix | 现有版本化审阅、分析与导出的输入，不随最新 working 状态静默变化 |
| Feedback / revision | 用户意图、对象、来源和版本绑定，修订局限于关联范围 |

表格、原文与派生内容不能通过 Markdown 拼接或临时 UI ID 冒充来源。
适用性、来源身份、坐标与哈希由工具验证，不让模型填造。

## 请求与反馈

- 业务请求始终绑定项目和文档；版本/节点操作再绑定相应版本和对象。
- 用户反馈必须带当前对象/版本；旧响应或旧反馈不能落到另一文档。
- 只有通过当前来源/覆盖/版本校验的工件可发布；失败只影响对应新结果，已接受结果保持可读。
- 公开 Activity 只包含安全的行动、范围、状态和回执；完整客户内容与秘密只进私有授权存储。
- 相同来源不因临时路径/会话 ID 变化变成新来源；业务 ID 不用显示顺序重编号。
- 默认可逆建议与人工最终决定分开；批量或不可逆写入遵守现有确认和校验规则。

## 现有入口范围

基础运行可查 `GET /api/runtime-readiness`；Agent 能力可查
`GET /api/runtime/agent-workflow-readiness`。

Reading、原文结构/native-reference、条目、Matrix、分析和导出分别由所属 router 提供。
正式应用中的F06仍只有两章原文结构工作结果，不能从“端点存在”推断自动全文producer、条目分类、
派生拆分或所有单元格交互已完成。阶段一的F06全文与全类型证据盘点是隔离工件服务，
不属于上述生产router，也不能用其页面可用性反推正式API已接入。

## 分析与导出

在线 Matrix 与导出使用同一冻结版本、层级和来源顺序。
人工结论优先，合格的当前终态 AI 可作为明确标注 provenance 的默认结果；
未终结、过期或无证据结果不能冒充当前有效结论。
FORMAL 必须满足现有锁定、就绪和有效结论规则，新 Agent 基线贯通下游另行验收。

## 执行边界

阶段一可采用轻量工件桥接核对工作台，不要求先注册全部生产端点。
阶段二冻结跨宿主业务工件，阶段三让 App 接入同一能力核心。
见[开发计划](需求提取/开发计划.md)、[能力设计](需求提取/架构与技术设计.md)和
[验证计划](需求提取/测试与评测计划.md)。


## 原文候选的主UI适配（2026-09-06）

阶段一使用target_review的可选`--frontend-dir`模式，将已生成的私有候选投影为主应用既有Matrix读协议，并复用相同UI组件；不写入或假称已生成生产项目、文档或Requirement Set。`reviewCase`仅选择这一显式候选入口。

节点新增可选`classification_paths`（系统功能/质量/约束、项目、过程）、`topic_tags`和`candidate_review`元数据。缺少这些字段的旧读模型保持原行为。验证关联与来源身份不被主题标签替代。

反馈仍经ReviewStore的`/api/review`追加保存，要求来源revision、反馈revision及同源令牌；原文不可覆盖。导出接口`/api/candidate-review/export`消费UI直接命中ID及版本，按冻结树顺序生成原文核对清单，不产生符合性结论。显式SOURCE_TABLE原表按实际列及合并格另存工作表，表头不计逻辑记录数；两列标准引用清单保留参考依据表。详见[target_review运行说明](../prototype/target_review/README.md)。

候选原文保真字段：sourcePresentation提供原文标题范围/编号；sourceExcerpts提供逐行字符范围。Matrix响应可含source_title_role与source_excerpts（直接从来源行切片生成的text、页码、DIRECT/CONTEXT角色）。这些均为可选字段，不向正式项目数据库新增业务实体。原文标题不再取未标注的语义概括；无原文标题的逻辑拆分可通过AGENT_ASSIGNED显式标记拟名/编号，UI及导出均标注Agent来源；Excel追加原文标题/摘录、派生依据原文，并保留来源层级的父编号。

候选元数据可用analysisEligibility=NOT_ELIGIBLE表达当前不进入自动分析；它不改动requirementness或classification_paths。适用/待定义状态与类别分别保留，分类筛选不能以自动分析就绪代替。

候选表格接口补充：reviewTables映射到logical_tables的structured_segments及projection.records，绑定真实行/列表头与图像；原文表行仍为SOURCE_ITEM，派生仍为DERIVED_REQUIREMENT。文档及来源页同时提供source_sha256/source_identity，`GET .../extraction/source-crop`只接受当前来源指纹和有效页内矩形，返回固定PDF的PNG区域。XLSX主清单不重复表格辅助行，原表/原图另附。

当前新增`reqpilotx-review-loop-context/v1`为只读Agent输入：绑定候选revision、有效feedbackRevision、原文定位、工作判断与原版本历史认可，复用已有`/api/bundle`和`/api/export`。不包含写入令牌，不定义新的业务真相或发布API。

## 2026-09-10 文档任务公共状态补充

`delivery/loop` 与 `delivery/extraction-loop` 的公共任务视图增加可选 `goal`（已保存的用户任务目标）和 `updated_at`（最新job更新时间）；来源/任务校验与既有控制合同保持。前端统一任务区复用这些字段，未新增模型推理接口。事件接口仍是字节游标增量GET；前端终态首次读取顺游标补齐至当前末尾，再显示最近记录。阶段MAP/ITEMS仍按来源与成果hash读取，结构确认仅作用于指定Map。完整变更与验证见[工作台首轮记录](需求提取/Agent原生工作台首轮实施与验证.md)。


### 2026-09-11 原生消息流与交付版本

在 `/api/projects/{project_id}/sor-documents/{document_id}/agent-structure/delivery` 下：

- `GET /loop/{task_id}/stream`（及 `/extraction-loop/{task_id}/stream`）：必传 `source_sha256`；`after`/`Last-Event-ID` 是 JSONL 字节游标。返回 `activity`（包含公开事件、首帧消息快照与来源绑定）、`complete` 或 `unavailable`。公开事件含 `message_id`、`version`、`event_cursor`；原事件 GET 默认不返回增量，仍可轮询兜底。
- `GET /versions/preview`：返回当前来源、完整交付 SHA、业务依据 SHA、原文/响应版本、沿用矩阵规则的就绪情况。
- `GET /versions`：读取该文档的保存历史。
- `POST /versions`：`command_id`、`expected_source_sha256`、`expected_delivery_sha256`、`expected_business_basis_sha256`、`mode`。FORMAL 另需 `accept_snapshot=true` 且有效响应完整；冲突返回409。相同命令/内容返回同一保存结果。
- `GET /versions/{revision}`：读取不可变快照；`GET /versions/{revision}/export` 下载已保存且验证 SHA 的 Excel，后续工作版变化不会重算历史。

原生交付版本独立于旧 requirement-set ID；来源/派生/人工意见/有效结论语义继续共用当前核心。迁移0037及实际验证见[验证记录](需求提取/响应分析消息流与交付版本验证.md)。

### 插件版本接入既有工作台（2026-09-12）

`GET /api/projects/{project_id}/sor-documents/{document_id}/agent-structure/delivery/plugin-workbench/{session_id}/reader?revision=...`返回指定不可变版本的现有reference、outline、table previews、review context和reasoning视图；`records/{source_record_id}?revision=...`复用共用条目详情投影，保留原文、原图、释读和明确关联条件。此适配不发布应用工作基线、不启动原生任务。结构投影`DRAFT`明确表示插件草稿，不能称人工接受。前端版本、反馈对象与导出均绑定同一传输版本；旧版反馈不得自动改投新版本。

### P1/P2首批读取扩展（2026-09-12）

原生outline与plugin reader.outline增加同源原编号`source_number/source_numbers/source_number_role`及`scope_summaries`（按node_id键）。统计明确直属/完整子树SRC、需求/非需求、派生与替代父项后的实际分析单元；`coverage.state=SAVED_SCOPE_ONLY`不声明原件全文完成。reader顶层同样提供scope_summaries，Map-only为null。

`source_findings`在reader/outline/Map reference中提供`source-findings/v1`范围报告：`scan_state`、`checked_scope`、`complete_or_unknown`、原件完整度null及带两端位置的findings。首轮仅检测已保存Map中的同级数字正文间断/重复/回退；状态`PENDING_SOURCE_REVIEW`，不自动修号。发现通用处置API未接入。

plugin公共GET补真实`goal`、work/producer引用、`execution.status=UNREPORTED`、成果同步时间、`capabilities`与`feedback_receipts`。支持保存反馈/确认Map/程序导出；未支持外部控制的能力显式false。回执中`retrieval_state=UNREPORTED`，不能由修订报告推断实际取回；旧反馈/版本字节不改变。

### 导出快照摘要的两个口径

插件导出的`X-ReqPilot-Snapshot`引用快照内`delivery_sha256`，其计算排除该字段自身；Excel隐藏元数据`SnapshotSHA256`对包含`delivery_sha256`在内的完整已保存snapshot计算canonical hash。二者不是同一种摘要，不能直接互相比等来判断错版。核对时分别使用对应口径，并同时确认下载revision/source绑定。UI独立只读复核v2时已得到两种摘要各自精确匹配；详见私有`prototype/runtime-data/ui-full-audit-20260912/download-verification.json`。
