# PAIR 固定业务流程与任务模型选择

更新：2026-10-31。本文记录固定流程的当前实现，不声明真实模型、原生浏览器或业务语义验收已完成。验收范围由 [PAIR 迁移规格](https://github.com/LiuChang0225/PAIR/blob/codex/pair-integration-20261027/docs/pair/development/reqpilotx/spec.zh.md)拥有。

## 入口与资料引导

共享 `HomePage`、`WorkspacePage`、`HostedAgentPanel` 与 `ApplicationHost` 继续使用原工作台。PAIR Client 仍注册主面板和侧边栏；发现登记由 **Host**
适配器通过可选 `ctx.pairSkillCenter.registerApplication` 完成，使用
`id=reqpilotx`、`stage=requirement-analysis`、`panelId=reqpilotx` 和实际
应用包版本。`readStatus` 只反映 Host 管理的实际 workbench 状态，不表示模型或业务
结果已接受。缺少 skill-center 的 raw DSH 仍保留侧边栏；Client 不再轮询
`/config` 仅为完成登记。

文档起始页和任务面板显示：

- 必需的客户需求文档；
- 可选的 PAIR 项目参考资料（本项目专属库）与团队知识库／保留主题版本；
- 固定流程预期成果和阶段顺序；
- 可选的补充说明和“先核对结构”选项。

PAIR 宿主存在 `ctx.knowledge` 时，项目参考上传走
`importProjectReferenceSource`，不请求 Wiki；文件、解析状态和失败重试如实显示。
项目库由稳定应用身份和项目 ID 通过 `ensure/getProjectReferenceLibrary` 获取并回读校验。
团队库只出现在非 application-project 的所有者范围；UI 隐藏不代替 Host 校验。

用户只需开始一次。补充说明保存在任务意图中，不能取消阶段或成果检查。任务启动后，
固定流程简报要求先恢复已挂载的 typed MCP 任务（`task_open`，`mode=resume`，
使用绑定 `task_id`），再按返回的 revision、selected、`next_action` 和读取句柄继续；
不以 CLI `--help`、运行时实现检查或编辑回执作为业务步骤。发送给 PAIR 的实际
启动 prompt 直接包含已准备任务的 `task_id`，首次 `task_open`（`mode=resume`）无需
先搜索目录；首次恢复后可按需读取 `request.md` 中的用户补充说明。公开操作无法恢复时
保留工件并报告确切限制。

本地 SQLite 版本不运行旧的原生 durable loop queue。`extraction-loop/latest` 和
`loop/latest` 在队列未装配时返回明确的 `NOT_STARTED` 与
`UNAVAILABLE_LOCAL_RUNTIME`，因此空文档显示“尚未发起任务/尚无原文工作版”，
而不是持续“任务状态暂未同步”。其它真实读取失败仍保留错误状态；显式空任务在
共享 UI 中显示为“尚未发起任务”。

## 操作失败历史与当前成果

任务操作日志区分已知终态失败和仍未决的操作。`FAILED`／`BLOCKED` 保留其冻结输入、
错误和工作目录，作为失败历史，但不会成为已选成果；后续合法操作提交且当前来源、
Map、条目、review、程序检查和交付仍成立时，这些历史失败不再阻止 `ready_to_review`。
`STARTED`（结果未知）、`SAVED_PENDING_RECOVERY`（已有回执待恢复）以及候选/冲突
引用仍按原 owner 保持可见或阻断。状态读取、`task_recover` 与 TASK.md 使用同一
操作日志，不删除旧操作、不补造回执，也不把未知结果标成完成。

## 任务证据绑定与工具

启动任务前，Host 按用户明确选择生成冻结绑定：

- 项目参考 caller 只选择该项目库及其明确文档；团队实时文档 caller 只选择该团队库及其明确文档；
- 保留主题使用独立 caller，只选择该团队库和精确版本，不读取该库实时原文；
- 组合场景把项目与主题放在两个独立 caller 中，同属一份任务 binding，不继承其他会话选择；
- 绑定记录原文 SHA、解析版本、覆盖、物理页／逻辑段位置和 Wiki 状态，并写入任务上下文；任务创建键和指纹同时冻结选择，之后变化不会静默改绑。

任务证据 MCP 工具（`reqpilot_knowledge`）只解析冻结绑定：
`knowledge_evidence_search`、`knowledge_evidence_read`、`knowledge_topic_read` 返回带
`fact_id`、原文引用、页码／逻辑段、覆盖和版本的摘录；每次调用由 Host 记录消费回执。

用户在项目/任务 intake 选择已由 Host 注册的 adopted knowledge library 时，Host 在
准备任务前冻结精确 `libraryId`／`versionId`，并只通过
`adoptedLibraries`／`bindAdoptedSession`／`adoptedBinding` 为该任务建立独立 caller
（`reqpilotx-task:<app>:<command>:adopted`）；不复用原生 Session，也不覆盖项目、团队
或保留主题 caller。Host 通过可选 `knowledge` 服务注入消费桥；迟到注入会挂载同一桥并更新可选输入
可用性，不会把缺失服务伪造成可用。任务证据 MCP 另提供
`knowledge_adopted_status`、`knowledge_adopted_browse`、`knowledge_adopted_read`、
`knowledge_adopted_evidence`，所有读取前后都核对冻结绑定和当前注册版本；版本变化
返回明确 `version-changed`，不回退 latest，也不读取包路径或 `dataDir`。

adopted 证据中的 claim、Wiki 和来源索引摘录是**派生记录**，不证明关联原件已被读取；
experience、conflict、gap 保持各自语义，原始可用性独立于提取和 review。实际读取
source 时 Host 校验实际 bytes 与返回的 `contentRevision`；metadata-only、missing 或
不可用原件明确失败，不伪造文本或页码。`sourceVersion` 保留被接纳文件哈希，
`originalSourceVersion` 只是原始声明，不是读取证明；逻辑段仍为 logical-section。
`review.expertApproved` 原样保留（boolean|string），只有布尔 `true` 是声明，不自动
成为项目适用性、符合性或工程批准。文件可读或 bytes 与 `contentRevision`/来源文件哈希
一致只证明读取完整性，不证明文档受控、项目适用性或工程批准；adopted 事实默认
`UNKNOWN`／`REFERENCE_CONTEXT`，不会因为读取了原件而升级权威。派生记录仍进入
引用、矩阵与 Excel 的独立“知识记录引用”投影；旧的“内部原件引用”只保留既有
原件类事实，不把记录表/派生文件哈希写成原件 SHA。消费事实进入同一 Host 消费账本和累计
attestation，模型不能伪造或隐藏旧决定；完整分析范围仍不能因批次缩小。当前切片
adopted 选择与旧项目/团队选择互斥；旧来源和保留主题路径继续工作。
`knowledge_analysis_save` 必须携带模型观察到的精确任务 revision 和稳定 command_id；
Host 从自有消费账本生成目录并签发绑定应用／项目／文档／任务／工作台／版本／决策的
attestation。没有消费事实、引用未被工具返回、旧 revision 或缺少 Host 签名的请求拒绝。
证据文本始终是数据，不是权限或指令；不可用、已变化或越界请求显式拒绝并保留原工作。
历史分析是部分结果时，固定流程仍须等待当前基线／派生范围内的每个必需分析单元都有
当前依据的结果，不能以 batch unit_ids 缩小任务范围。Host attestation 对累计结果集签名；
工作台上传必须让每个已保存 assessment 记录都由该 Host 签名集认证，不能只校验最后一批。
原有 partial 合并、无关历史记录和人工决定保留；attestation 的 workbench ref 取自校验过的
task-owned binding，不读取不存在的内部状态字段。保留主题使用真实 provider
`TopicVersionInfo`／`TopicReadResult` 字段：availability、verification、applicability、
origin、精确来源 document/version、physical-page 或 logical-section 的 number；
派生 topic 内容不伪装成原始来源事实，原始引用通过 `topicOriginal` 解析。live 原件
引用还会校验冻结的 original/extraction revision 与实际下载字节；变化或不可用时返回
明确 changed/unavailable，不返回新字节冒用旧分析。logical-section 仅使用 section
定位，不生成伪页码。

## 服务端固定流程

`reqpilot-fixed-workflow v1.0.0` 的版本、定义摘要、阶段顺序和启动 brief
由冻结能力包提供；Host 不编写业务目标。`host_launch.py` 组合固定目标和
可选补充说明，把工作流规格写入任务上下文。阶段顺序：

1. `PREPARATION`：任务上下文、原件字节与能力包；
2. `SOURCE_MAP`：已保存 Document Map；
3. `ITEMS_CLASSIFICATION`：已保存条目、分类及明确范围的派生；
4. `EVIDENCE_ANALYSIS`：有冻结内部依据时必须保存实际分析建议；无依据分支 `SKIPPED`。
   仅有选择、导入或策略声明不算分析结果；建议进入原分析工作台和同版 Excel；
5. `SELF_CHECK`：绑定当前来源、Map、条目、review 与交付的程序检查成功回执；
6. `DELIVERABLE`：与当前条目同版的已保存交付。

`fixed_workflow.py` 只从这些已保存工件和分支声明投影状态。模型文字、会话
空闲、导出动作或补充说明都不能推进阶段。Task inspect 通过固定 TaskService
重新投影；Host 不把任务目录中的 `workflow-status.json` 当成权威证明，而是
通过固定包的 task inspect helper 重新读取，并校验 task/source/workflow/
brief、阶段顺序、SAVED 证据和 readiness。缓存损坏、外来任务或校验失败时
显示“状态未确认”并保留已保存工件，不回退到伪造 ready 或静默重置。

插件工作台的 `fixed_workflow` 入参同样只作为候选声明：服务端忽略发送方的
阶段状态和 `ready_to_review`，只根据已验证的 Map、条目、派生、分析策略及
当前同版交付版本回执重新推导。六个 SAVED 但没有工件、空证据、矛盾
readiness、旧 basis 或损坏交付回执都不能得到 ready；无内部依据保持
`SKIPPED`。经实际编译、复核并保存交付版本的完整流程仍可得到 ready。当前
`publish_current_review` 会为已通过的当前检查预览精确快照，并通过现有
CAS／幂等交付接口注册或复用一份 DRAFT（`acceptance=null`，不创建 FORMAL）。
绑定任务的 `task_deliver` 通过同一路径完成工作台交接；未绑定任务保持本地。
手工意见或远端版本变化使用精确冲突拒绝，不回退 latest，也不重编译来源。
历史任务没有工作流上下文时不生成该状态。`ready_to_review` 与 Pair 执行状态
分开显示，仍不等于人工接受。PAIR 托管视图使用原生 Session/Workspace 状态，不再用
“外部运行状态未报告”替代可读的会话状态；独立插件面板仍保留其外部运行语义。
新任务的 PAIR Workspace 注册标题使用文档名生成可读标签，并调用实际的
`workspaceController.rename({workspaceId,title})` 请求对象；已有任务目录和 UUID
标识不重命名，重试沿用同一路径、Session 和任务身份。PAIR 托管视图中新引入的
固定流程状态、流程说明、固定可选输入/预期成果说明、任务执行状态和任务控制文案
通过槽位提供的原生 `t` 绑定读取宿主语言；未知自定义输入保持原文，独立 Web 默认
保持中文。源标题组合只合并属于编号边界的重复分隔符，保留括号、
方括号、`.NET` 等有意义的原文标点。

相同检查内容保留已有回执身份，重复检查时间不会产生新成果版或重复 DRAFT。部分成果可以保存和下载，但未完成声明范围时不报告固定流程交接完成；交接还要求最终远端和本地版本均匹配。没有固定流程上下文的 portable 任务保留其原有来源与工作台内容验证。

Map 确认、无内部依据和显式派生是声明的分支；Map/items/delivery 原有校验
继续拥有前置条件。没有 Map 保存条目、错误来源或错误工作流版本都会被拒绝。

## 成果后修订与交付

在已保存 review 后，原生会话中的只读解释不写业务数据；明确的派生和局部修正
继续通过同一 TaskService／review CAS 路径保存。类型化 `review_apply` 现在提供
`changes.map_revision.decisions`：程序复用既有 Map 编辑器编译候选，在 review
内保存候选的原始来源决策，再通过现有 Map lineage／review-revise 编译、导出并
关闭一个新版本；不另起 Map 或 review 流水线。来源身份、未受影响条目、评估和
正式人工意见由现有 carry／rebind 逻辑保留。任务 revision、review head 或来源
不一致时拒绝覆盖并返回重新读取当前任务的恢复动作，不自动改绑最新版本。
会话中的文字和模型建议仍不构成正式人工接受。`workbench_entry` 返回稳定
`task_id`／`work_ref`／`result_revision`／`session_id` 和“选择原工作台”动作，
不把 Session 聊天入口冒充工作台；`chat_entry` 只在宿主提供已核验的原生 Session UUID 时使用，可移植工作台不从业务
`work_ref` 推导或伪造 PAIR 会话身份。
`workbench_url` 只是托管服务传输地址，服务重启后可能失效，不作为长期入口。
绑定任务登记或复用 DRAFT 时，`task_deliver.delivery_handoff` 把该保留版本的
精确导出下载到任务自有目录并核对来源、版本、snapshot 与 export SHA；该副本
在托管后端重启后仍可交付。程序本地 `matrix.xlsx` 与服务器保留 DRAFT 可能在
文件名、交付身份或时间戳上不同，未取得精确副本时不得声称字节一致。

PAIR master 的 `session.create` 只复用指定且空白的 Session，对已开始的 Session
可能新建身份。Host 在重试/重启时先用 `sessions.inspect` 核对已有 Session：
不存在才按已知身份创建；空白可复用；已开始、忙碌或已完成则不再调用 create，
而是核对 cwd／workspace／冻结模型后通过原 Session 续作。忙碌且没有精确请求
记录、模型未确认或工作区不符时拒绝，不重复建 Session，也不静默切换模型或
工作区。

## 模型、请求与恢复

新任务使用 PAIR 当前选定的默认模型和推理档位。启动记录
`reqpilot-pair-launch-record/v3` 在派发前保存 `modelSelection`，在会话创建后
通过 `sessionController.selectSessionModel` 确认并保存
`selectedAgentModel`，然后才提交 prompt。显式部署 `agentModel` 仍作为覆盖。
缺少默认模型或会话模型选择能力时显示可操作的设置状态，不启动独立运行时。
bundle 不再固定 Codex 模型。

v3 任务返回 `reqpilot-pair-launch/v2` 公共 receipt，包含绑定来源的固定流程
和 `workflowState`。旧 v1/v2 启动记录与旧 receipt 保持原解释；新 Client 拒绝
缺失工作流的 v2 receipt，新 Host 拒绝未知 receipt/工作流版本。同一请求键、
同一 Session、同一 Work/Source 的重复提交、停止、继续和显式重连沿用原
任务，不另建业务台账。

## 本地验证边界

Host 登记使用 PAIR 源码检出中的真实 Cordis service/fiber 验证注册、运行状态变化
和随 fiber 释放。原生 Session 使用源码支持的 `pair` profile、隔离 `DSH_HOME`、
仅回环地址的 OpenAI-compatible 脚本端点和该路由的显式批准；真实
SessionController/Workspace/MCP 完成 `task_open`、`review_apply` 的有界 Map 修正和
`task_deliver`。脚本文字没有推进未保存阶段；Map 修正和交付只在真实工具返回已保存
回执后成立。该证明是 keyless/scripted，不是真实 Provider 或业务验收。
源码检出缺少预构建原生 system addon 时，runner 只把安装包中匹配的预构建复制到
私有输出并记录该精确依赖，不修改生产策略或用户 profile。

## 未覆盖范围

- 真实 Go Max 任务、原生 PAIR 浏览器和真实客户文档由父任务验收；
- RX03 的成果后解释／派生／局部 Map 修正与同版交付已通过本地公共任务路径验证；
  真实 Provider 和原生浏览器观察由父任务运行新的脚本化 Session fixture；
- RX04/05 消费端在本分支实现源码与脚本化验证；真实 PAIR Go Max、浏览器和客户语义验收仍由父任务执行。
  保留主题 JSON 的具体字段按已交付 provider 合同解析；不同 PAIR 构建的字段差异仍需实际运行核对。
- 冻结能力包准备另使用合成 HTTP 与脚本化 Session 控制器；真实 DSH 原生
  Session 证明只覆盖 keyless 脚本路由。证据消费／版本变化的 keyless 原生 Session
  场景见 `integrations/pair/scripts/dsh-knowledge-native-snapshot-probe.mjs`，仍待父任务在本机 PAIR
  源码与真实构建上执行。
- adopted consumer 目前由合成 knowledge 服务、合成版本身份和聚焦 Python 合同测试
  验证；未运行真实 IRPS 包、真实模型或真实浏览器选择/引用点击验收。注册仍由父任务
  的可信 Host 操作完成。
