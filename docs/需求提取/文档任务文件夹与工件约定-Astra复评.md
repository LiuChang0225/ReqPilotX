# 文档任务文件夹与工件约定：Astra 独立复评

更新：2026-09-12。范围：用户指定的 Astra ultra **方案复评**，以已完成的 Sol high 评估、P1/P2 实施记录和对应代码为输入；不是原件提取模型对照。本轮只更新本文件和[评估入口](文档任务文件夹与工件约定评估.md)，不实施代码、数据库迁移、客户目录搬迁或服务操作。后续顺序仍由[开发计划](开发计划.md)维护。

## 结论与需要纠正的方向

**应直接采用“项目内有文档目录，文档内有可接续的任务目录；程序负责固定工件位置和状态索引，Agent 在其中自由阅读、编制与修订”。下一增量应交付一个真实可用的任务工作区，而不是先建设通用工件数据库。**

Sol 对来源保真、版本分离、程序维护索引和同版前端读取的判断基本正确，但把首增量写成 document family、通用 artifact/result/member/head 表、注册服务和双适配迁移，明显超过用户建议所需。其“更简单替代方案”进一步优先不提供长期可见的原生文档目录，容易把用户需要的执行环境削弱成数据库的临时展示。**不采纳这一实施顺序。**

主任务后来决定“不先迁数据库”是正确的；但仅采纳“类型字典＋机器索引”仍不够。必须同时交付：真实任务根、明确目标与当前范围、程序自动选址、已保存结果及原件的可达路径、前端绑定、失败后的接续入口。否则 Agent 仍要自己拼大量路径、猜哪个结果有效，用户的主要问题并未解决。

推荐的最小组合是：**小型代码工件字典＋每任务机器维护的工作区索引＋现有 review/plugin/native 持久化适配**。不新增通用 DB 表；不把文件存在当成成果完成；不把数据库登记当成所有已可核对成果的新增门禁。保留现有源模型、编译器、版本链和 UI 外壳，优先补下面已经核实的接续缺口。

## 相对 Sol 方案的接受、修改与拒绝

| Sol 建议/主任务采用项 | 复评决定 | 依据和具体调整 |
|---|---|---|
| 固定工件类型，不固定语义读取步骤 | 接受 | 工具决定落盘合同，Agent 决定阅读、回查、粒度和内容判断。目录不编号为必须依次完成的六步 |
| 程序初始化、维护 hash/head/index | 接受并补足 | 还应自动解析输入版本、分配输出位置、返回接续信息；不能仍让 Agent 手写完整 manifest 或任意目录名 |
| 一个项目多个文档，每文档可有多次运行 | 修改 | 必须增加持续的 **task/work 层**。任务可以跨多次运行、反馈与会话；run 不能直接代替任务文件夹 |
| 原生文件夹只是按需物化投影 | 修改 | 物理存储可继续按需物化，但任务运行与恢复时必须有可直接读取的实体工作区和稳定入口；不能把这项交付无限延后。旧 native workspace 本来就已存在，无需另造一棵内容副本 |
| PostgreSQL＋object store 是全部注册真相 | 修改 | 当前是按对象分责的混合存储：上传、working revision、交付有 DB；native task/stage 和 plugin 会话/版本有宿主管理的文件注册。尊重各自真实 owner；不要因没入新 SQL 表就称已上线插件成果“未注册” |
| 统一十类身份并从第一天建立 family/unit 实体 | 修改 | 本轮用真实 project/document/source SHA、task/work、已有 producer 和版本引用即可；family 与独立 unit 注册延后，但原件不可变和正确多根结构不能延后 |
| 新建四张通用 artifact/result/member/head 表进入首切片 | 拒绝作为前置 | 现有不可变结果集合和读接口已可复用；新增表同时引入回填、双写、GC 所有权、故障补偿等工作，未证明它们比当前缺口更急 |
| 三份文件清单和 STAGES 全部消费统一注册表 | 修改 | 可以共享类型名、文件名和校验依赖；**传输允许集、review 组合合同、阶段投影不能被合成同一份“必需文件列表”**。例如 Map-only 合法，plugin 尚不支持完整人工/证据传输 |
| 所有类型都按 artifacts/kind/version 单独存放 | 修改 | 初期保留编译器和 review 已有的同版 bundle，索引用类型定位其中的文件。拆散伴随表格、收据、知识证据会破坏既有相对路径和重放依赖 |
| 文件已写、已注册、可展示、已验证、已接受分开 | 接受并澄清 | 这些是独立事实，不是一条必须经过新 DB 的升级流水线；验证还要区分确定性校验、来源核查和人工接受 |
| 目录物化 CLI 在注册数据库稳定后再增加 | 拒绝 | 用户要验证的正是工作区能否降低机械工作和接续成本；应以实体目录和实际 CLI/工具接续先验证合同 |
| 多 producer 不按 revision 数字/时间静默合并 | 接受 | 当前 native/plugin 的版本和执行身份不同。统一列举与显式选择即可，不先建设自动分支合并 |
| 项目知识共享、分析冻结实际版本 | 接受 | 复用已有知识登记与导出校验；副本是来源绑定的冻结材料，不是可编辑的“最新知识” |

## 一手事实：已有能力比原评估承认的更多

本节是对当前代码的核查，不把实施记录或 active-context 自身当作独立证明。代码行号以复评读取时为准。

| 事实 | 一手代码与含义 |
|---|---|
| portable 已有实体冷启动 workspace | [reqpilot.py](../../plugins/reqpilot/scripts/reqpilot.py) 的 `bootstrap`（91–135）复制冻结包、挂载 skills、写原件清单与 request.md；支持多 `--source`，但输入平铺，没有文档/任务身份目录，也不能在同一已初始化根重复 bootstrap |
| portable 已有可恢复 review | [revision_tools.py](../../plugins/reqpilot/scripts/revision_tools.py) 的 `open_review`（240–273）、`write_head`（84–108）、`current_bundle`（170–178）、`close_review`（614–674）已有原件副本、版本 hash、反馈前驱、OS 锁和原子 head 替换。不能从零重建同义系统 |
| review 不是冷启动全流程工作区 | `open_review` 要求已有 Map＋baseline＋receipt；`close_review` 要求匹配的 Excel 交付（641–647）；`verify_review` 没有交付会拒绝（726–727）。它不能单独承担 source-only / Map-only / 未导出局部修订的所有阶段状态 |
| native 已有真实任务根 | [source_item_loop_jobs.py](../../prototype/app/application/agent_workflow/source_item_loop_jobs.py) 的 `_task_root`（99–107）使用 `object_root/runtime/native-loop-tasks/<scope>/<task_id>`；[source_document_loop_jobs.py](../../prototype/app/application/agent_workflow/source_document_loop_jobs.py)（370–437）保存 source、knowledge、task-input.json、task.json 和冻结 plugin |
| 来源与对象已有登记 | [0002](../../prototype/migrations/postgresql/0002_business_schema.sql) 的 `sor_documents`保存项目/原件SHA/storage key，[0003](../../prototype/migrations/postgresql/0003_storage_and_cutover.sql)关联 `stored_objects`；[source_item_agent_jobs.py](../../prototype/app/application/agent_workflow/source_item_agent_jobs.py)（581–612）按作用域/摘要写对象，读取验证key范围、大小与SHA |
| 原生正式发布已有事务边界 | [postgres_jobs.py](../../prototype/app/adapters/persistence/postgres_jobs.py)（1104–1203）在任务成功事务内校验lease、source、Map与前版；对象已写但发布冲突时保留未发布。Map working revision已有[0034](../../prototype/migrations/postgresql/0034_agent_structure_working_revisions.sql)的不可变版本/CAS head，交付已有[0037](../../prototype/migrations/postgresql/0037_source_item_delivery_versions.sql)的文档内版本与command唯一约束 |
| native 已有任务简报和发现入口 | [native_task_brief.py](../../prototype/app/application/agent_workflow/native_task_brief.py) 的 `build_task_brief/render_task_goal/save_task_brief`，以及 [native_loop_tools.py](../../prototype/app/application/agent_workflow/native_loop_tools.py)（237–283）的 `reqpilot_context` 已返回目标、实际阶段、来源、工件导航、可用操作和读取目录。不应新增另一套静态全流程 prompt |
| native 的执行 cwd 与材料目录并非同一个根 | `native_loop_tools.py`（264–281）已明确区分；[native_loop_driver.py](../../prototype/app/adapters/agents/codex_app_server/native_loop_driver.py) 为 skill/runtime 建独立 cwd。新增索引不能靠全局 chdir 把两者强行合一 |
| plugin 已通过应用 API 注册并展示版本 | [plugin_workbench.py](../../prototype/app/application/agent_workflow/plugin_workbench.py)（75–132、204–267）校验来源、同版 Map/baseline/receipt，锁住 session，先写版本再推进 head；同版读取校验文件 SHA。没有通用 SQL 工件表，不等于没有应用持久化 |
| 已有同版读取/导出和统一外壳 | `plugin_workbench.py`（286–299、355–366）支持指定 revision；[DocumentWorkSurface.tsx](../../prototype/frontend/src/features/workspace/DocumentWorkSurface.tsx)（89–128）共用外壳和协作入口；[usePluginWorkbench.ts](../../prototype/frontend/src/features/workspace/usePluginWorkbench.ts)（48–63、98）固定查看版及导出地址。P2 首切片不能再作为“尚未实现的共同外壳”重做 |
| findings 已有首轮报告 | [source_item_workbench_metadata.py](../../prototype/app/application/agent_workflow/source_item_workbench_metadata.py)（19–67）实现 `source-findings/v1`，范围仅是 saved numeric body peers；[artifact_tools.py](../../plugins/reqpilot/scripts/artifact_tools.py)（493–497、780–791）保存报告。通用处置/核查状态持久化仍未实现 |
| 统计与拆分处置不是目录推导 | `source_item_workbench_metadata.py`（70–117）校验同源 Map/baseline 后计算；`artifact_tools.py`（812–828）传入明确授权范围并输出 review_scope。目录数或文件数不能作为完成度 |

已只读查看 P1 的 `live-data-verification.json`：保存了 v3 快照未变、原编号投影一致、Excel snapshot pin 一致、3 个历史版本、1 条反馈和 2.6→2.10 提示。这是**原执行任务的回执**，本复评没有重新启动 UI/模型或复跑这些链路。复评开始后另一个 UI 任务获得新授权，其后修改不能追认成 Sol 评估或 P2 首切片的既有成果。

## 身份与生命周期：本轮必须分清，但无需每项新增实体

这里“本轮”指建议的下一实施增量；本次复评本身不建目录或改代码。

| 情况 | 本轮必须做到 | 可以延后 |
|---|---|---|
| 一项目多文档 | 用实际 `project_id + document_id + source_sha256` 绑定；离线用程序分配的本地项目/文档 ID，日后显式关联应用 ID；显示名与目录名不作身份 | 跨项目文档合并、全局文件目录服务 |
| 一文档多任务 | 每个持续委托有独立 `work_id`、目标、范围、工作根和 host binding；恢复原任务不重新发 work_id；不同任务的已存成果并列 | 自动合并两个任务的语义成果；把所有旧任务迁入新目录 |
| 一任务多运行 | 保留实际 native task/job/attempt/thread/turn 引用；portable 只记录真实已知的宿主引用。程序命令调用有自己的 operation ID，不能冒充模型 run | 外部运行心跳/跨宿主控制；不知道 run 时允许 null/UNREPORTED |
| 多 producer | 保存真实来源类别与实例；本地 review 同步 plugin 时保留 local review SHA 与 plugin session/revision 的映射，不伪造两个模型运行；native 与 plugin 各保留版本 | 通用 producer 调度平台、跨分支自动 reconciliation |
| 原件新修订 | 新字节必须是新的上传/document 关联和任务源绑定，旧版本只读可达；相同 SHA 不能自动合并两个业务文档 | `document_families`、上传“同文档新修订”的完整 UX、跨 SHA 的 SRC 继承和反馈重绑定。可先保存明确声明的 predecessor 引用为元数据，不据此继承意见 |
| 一文件多逻辑单元 | 立即遵守模型 V1：真实独立附件与主文档平级，正文附录保持真实归属；任务范围引用 `map_sha + node_id/来源范围`，允许全文回查 | 只有需要独立授权、独立版本、跨原件延续或单独交付时再建 unit 实体。当前不强制每份文件注册一个“整份原件 unit”空壳 |
| 补读/多种 capture | 原件 SHA 固定，各捕获/补读版本及其文件 SHA、前驱、方法分别保留；reading 是可再生投影；不能用同一个 source_ir_id 掩盖内容变化 | 统一 capture 数据库或强制每页一次注册 |
| 共享知识 | 项目一次登记；每任务固定实际 catalog SHA、selected documents、策略及可读取证据。无内部依据明确 SKIPPED，不创建假空分析成果 | 全量知识资料重排、知识快照去重优化 |
| 反馈与核查 | 保留针对的查看版、source/Map/baseline、节点/SRC、origin 和处理回执；未取回、已修订、人工接受分开 | 全新评论平台；finding 的跨来源自动继承 |
| 交付与清理 | 固定同版结果和真实范围；任务结束不删除有效成果/反馈/证据链；只清本次失败且无后续用途的 scratch | 全量归档/自动 GC。不可用“临时目录”作为删除仍被版本引用的证据的理由 |

`artifact_sha256`（业务合同 hash）、`file_sha256`（实际字节 hash）、source SHA 和 result revision 是不同值。新索引应引用原值并注明含义，不重新计算一个“统一版本号”替代它们。

已有 native `task_id`、plugin `session_id`直接复用为各自命名空间的 work 引用，不再多发一个同义 UUID；只有新建、尚未绑定宿主的 portable 委托才需要本地 work ID。文件夹层次表达归属，不要求在每份业务 JSON 中重复全部身份字段。

## 最小目录方案

固定的是任务入口、类型及保存规则。下面是**建议新增的 portable 布局**，不是目前已有命令的输出。现有 `bootstrap` 继续负责一次安装/冷启动环境；它不应对每份文档复制一套插件。

```text
<private-working-root>/
├─ .reqpilot/                              # 已有：冻结的工具/skills，或引用已安装包
├─ .agents/skills/                         # 已有：宿主发现入口
└─ runtime-data/projects/<project-key>/
   ├─ project-index.json                   # 新：程序列举文档/任务，只有引用，可重建
   ├─ knowledge/<catalog-sha>/             # 可选：项目登记的冻结证据或可验证引用
   └─ documents/<document-key>/
      ├─ document.json                     # 新：本地/应用身份、显示名、角色、原件引用
      ├─ source/<source-sha>/original.pdf   # 原件副本；扩展名依真实格式
      └─ tasks/<work-id>/
         ├─ TASK.md                        # 新：由程序生成人可读目标、状态与接续路径
         ├─ task-context.json              # 新：任务目标/策略/绑定及其来源，非模型手填
         ├─ workspace-index.json           # 新：已提交工件、阶段引用、host回执/待同步
         ├─ source/original.pdf            # 按宿主需要物化的受控副本，仍绑定文档原件
         ├─ knowledge/                     # 可选：该任务实际可读的冻结证据/明确引用
         ├─ runtime-data/workbench-bindings/
         │  ├─ <source-sha>.json            # 已有binding格式；任务根成为显式绑定作用域
         │  └─ <source-sha>-inbox.json      # 已有取回快照，保留未适用反馈
         ├─ reading/<operation-id>/        # SourceIR、逐页MD/原图/原表、补读与阅读索引
         ├─ outputs/<operation-id>/        # 编译器原有同版bundle；完整保留内部相对路径
         │  └─ operation-receipt.json      # 新：成功校验后的提交回执，最后写入
         ├─ review/                        # 达到既有review-open条件后才生成
         │  ├─ source/
         │  ├─ versions/initial/           # 或后继候选，沿用现有编译/关闭合同
         │  ├─ reviews/<review-sha>.json
         │  ├─ workspace-manifest.json
         │  └─ deliveries/<operation-id>/
         ├─ runs/<known-attempt-id>/        # 可选：实际运行事实/公开回执，非每步骤一个run
         └─ notes/                         # 可选：有出处的观察/交接，不作状态权威
```

开始时只建入口和实际需要的目录；不预建十几个空成果目录。目录名中的 ID 由程序分配，Agent 获得具体路径，不需要照模板拼字串。`operation-receipt.json` 是新增合同的设计名。

共享原件/知识可以在项目层登记一次；受限native读取器仍要求材料位于授权workspace时，继续使用已校验的任务副本。不要靠指向父目录的symlink绕开现有路径检查。离线携带时校验依赖闭合，需要应用才能读取的引用明确标注，不能只复制一份index就宣称任务已完整打包。

原生任务**保留现有物理根**，在 `workspace/` 补相同的任务入口和索引语义；原有 `task.json / stage-results.json / task-briefs / controls / runtime / plugin` 不搬迁。项目/文档索引可以指向经过宿主校验的现存 task workspace；不为整齐复制全部客户材料。后续新建路径是否采用上面的层级并不影响合同，索引必须返回真实 `workspace_root` 和 `execution_cwd`，两者可不同。

这些目录是 Agent 的执行与恢复接口。应用已接收的成果由原有 host owner 负责持久化，前端仍通过 API 读取，不能任意访问浏览器外的磁盘路径。portable 离线成果用本地提交回执和 review manifest 管理；应用导入验证成功后增加 host receipt。两种登记范围明确分开，但不要求后者必定由新增 SQL 表实现。

## 固定/可选工件类型与前端映射

文件名以当前编译器为准，避免重命名已保存工件。新类型字典保存 `kind / filenames / schema或校验入口 / dependencies / representation_role`；**是否本任务必需**由目标及实际能力决定。前端组件名只作映射说明，不应反向写进业务工件。

| 类型与实际文件 | 何时必需；生成/读取责任 | 前端落点与保存边界 |
|---|---|---|
| 任务上下文：`TASK.md / task-context.json / workspace-index.json`（新增） | 每任务必需；宿主/CLI 从真实目标、现有版本和回执生成；Agent 先读此处 | 文档任务摘要、固定协作区、已有成果/剩余范围/待用户输入；不展示一屏技术 manifest |
| 原件：实际原件＋SHA/注册引用 | 每任务必需；上传/初始化复制与校验，Agent 全文可达 | 文档库、原页对照；目录存在不代表来源已读 |
| 捕获/阅读：`source_ir.json`、补读版本、`reading-workspace.json`及其资源 | 按文档和方法需要生成；捕获/阅读程序负责；材料可复用 | 原页、原表/图详情、阅读入口；不增加第二套业务树 |
| Map bundle：`document-map.json / table-previews.json` | 有结构成果时必需；Agent 决策、共用编译器生成，Map-only 合法 | 结构树/原页对照，复用现有 StageResults/Reader |
| Map 重放证据：`map-compilation-receipt.json / map-decisions.json / map-source-ir.json / map-source/*`及编辑前驱 | 发生 Map 编制/修订时保留；程序记录真实生产链，Agent 不手写 hash | 默认不占主区；按需查看依据/诊断。索引必须保留整个依赖集合，不能只保留 Map JSON |
| 条目 bundle：`source-requirement-baseline.json / validation-receipt.json`及同版 Map/表、`item-compilation-receipt.json` | 请求且已产出条目时；编译器保存 covered/unprocessed scopes，Agent 判语义 | 台账、树、完整详情、分类与范围统计；PARTIAL 可展示 |
| 注解：`image-annotations.json` | 有释读/原编号/上下文等有效注解才需要；共用编译器校验 baseline | 详情和图像释读、原编号；原图和解释分开 |
| 拆分：`source-item-decomposition.json`及编制/授权回执 | 只在授权范围；KEEP/UNDO/SPLIT与理由、已处理/剩余由现有编译器保存 | 父项、Snn和处置区；0个子项不代表未检查或检查完成 |
| 分析策略/证据：`analysis-policy.json / knowledge-catalog.json / evidence-registration.json`及证据资源 | 策略必须可解释；只有实际知识可用时冻结证据；宿主登记、Agent 选择适用证据 | 分析区/协作区显示分析或跳过原因；plugin 不支持的传输不静默删掉 |
| 分析：`analysis-packet.json / source-item-assessments.json` | 有授权与内部证据才产生；Agent 提判断，程序验来源和证据依赖 | 分析详情、响应列；当前 plugin 完整传输仍明确不支持 |
| 人工意见：`manual-decisions.json`及来源/导入回执 | 有真实用户意见时必需保留；已有人工 API/导入器负责 | 工程师响应及交付；不能由Agent生成 accepted 状态 |
| 发现报告：`source-findings.json / map-audit.json` | 当前检测适用且已运行时保存；程序扫描、Agent 回原件核查；未检测明确未知 | 来源提示、两端定位、未决摘要。当前 UI 还可按冻结 Map 重新投影；这不等于读取同一个已持久化处置工件 |
| 反馈/修订：review history、inbox、`revision-diff.json / carry-forward.json / revision-receipt.json` | 有反馈或修订时；现有 API/review 保存，绑定原查看版 | 协作区、变化说明、未决意见；不是普通聊天完成标记 |
| 交付：`matrix-snapshot.json / matrix.xlsx / delivery-receipt.json`或原生 delivery version | 实际导出时；共用程序生成、检验确切 snapshot pin | 当前交付/历史交付与下载；明确草稿、范围和未决事项 |
| 操作/运行记录 | 保存真实工具回执、错误、尝试与前驱；大输出按需访问 | 固定协作区的真实活动；不把文件 mtime 当模型心跳，不保存隐藏思维链 |

类型字典不能把“可选且未产出”和“不支持且丢失”都折成空数组。建议每个请求的成果槽提供：`availability`（AVAILABLE/ABSENT/UNSUPPORTED/FAILED/UNKNOWN）、`scope`、`basis_ref`、`reason`；未请求/不适用/策略跳过用明确 reason。工件另外保存验证收据、发布回执和接受记录，保持事实正交，避免枚举组合爆炸。

例如现有 `DocumentMapArtifactV1.status`在模型中固定为`ACCEPTED`（`source_requirement_baseline.py:241–248`），而plugin阶段API返回DRAFT、`human_acceptance=NOT_CLAIMED`。类型字典不能把这个内部status直接投影成“工程师已接受”。

发现处置应作为绑定 `finding_id + document_map_sha256 + 检查范围/证据` 的独立事件或伴随工件；沿用用户反馈 owner，Agent 核查标清 origin。当前 finding ID 不含 Map SHA（`source_item_workbench_metadata.py` 52–55），因此**不能只凭 finding_id 把旧“已核查”自动带到新版**。本轮先完整保留扫描报告和现有反馈关系；专门的处置写入可接着做，不以通用问题数据库为前置。

## 从收到任务到清楚接续

1. **初始化一次真实委托。** 程序接收当前用户原话、真实来源、已知项目/文档、允许的范围和实际确认设置；从已有上下文能确定的字段自动填入。默认路径可自动选择，业务目标和原件不能凭文件名猜测。启动位置返回 task workspace，不把包安装目录当客户工作区。
2. **生成紧凑动态简报。** 复用 `native_task_brief` 的状态逻辑，使 portable 也能获得当前请求、来源、Map确认、已有成果和工作台入口；补读取 item receipt、decomposition review_scope、inbox、carry-forward 和真实运行状态。对未知剩余范围写 UNKNOWN，不用“已有17条”推定总量17。
3. **把事实与建议分开。** TASK.md 前半部由程序生成为“目标、正在查看/继续的版本、已有、剩余、需用户输入、可执行动作及准确参数”；Agent 的观察、方法与未决解释可以单列 notes 或显式自查反馈。原请求留档，新反馈按实际优先级进入 current_request，不静默改写历史目标。
4. **工具返回下一步所需材料。** 提交 Map 后返回该版本、具体路径、范围、扫描报告、是否待确认；条目保存后返回 covered/unprocessed scopes；导出后返回 snapshot 和下载。自动填写机械 ID、hash、路径、binding；Agent 只输入语义决定、真实依据和有业务意义的范围。
5. **用不增加工作流门槛的方式恢复。** 新会话读 TASK.md 并刷新状态，即可继续原工作；未保存的推理不能宣称恢复，已保存的有效 Map/Items 不因 run 失败消失。遇到版本冲突时保留两个候选并说明冲突，不静默合并。

现有 `reqpilot_context` 已做了相当一部分；应提取共同的**事实投影**，而非把很长的 native 提示词复制进所有目录。程序能解释阶段、必需输入和真实授权点，不能仅因产物齐全就断言语义完成，也不能把“推荐下一动作”变为唯一合法路径。

保持自由度的验收反例：Map阶段可以重新阅读任意授权页、补图/文字证据、跨章调查；条目反馈可以回修 Map；不同格式可换读取方法；有有效局部成果就能保存。仅当使用某项工具本身缺少必需来源/版本，或用户明确要求先确认该版结构时才限制相应写动作，不禁止继续阅读与自查。固定文件夹不意味着强制每章一个 Agent、每页一个任务或每阶段新开模型。

## 保存、注册、并发与失败恢复

### 用一个小型提交适配层承接现有编译器

`artifact_tools.save` 使用 `open('x')`，`new_output` 拒绝已存在目录（23–39）；一次 compile 逐文件写 bundle。它防止普通覆盖，**不提供整组文件的原子提交**。建议新增 workspace wrapper，保持既有编译器业务合同：

1. 程序分配唯一 operation ID 与最终私有输出路径，记录输入的确切工作/来源/前版、实际命令和能力包 SHA；编译器向新目录写入。
2. 成功后按类型校验整组依赖、来源和文件 hash，最后原子写 `operation-receipt.json`。只有完整回执的输出进入 committed 列表；失败目录保留诊断，不自动升为成果。
3. 在任务短锁内检查 expected index generation / 既有 review head，原子替换 workspace-index。review 已存在时，`current_working` 必须引用真实 review head；未 close 的候选明确为候选，不能让 workspace index 另立一个“已接受 current”。Map-only 的本地阶段指针可由同一适配层管理。
4. 需要应用可见时调用原有 workbench/native 发布适配；保存成功和同步成功分别记录。断网不回滚已保存成果，收到应用回执后更新映射；重试不改 command ID 或擅自采用远端新 head。

native的受信工件记录、真实producer调用回执和最终来源重放继续生效；新index不能把任意磁盘文件标成可信工件而绕开现有宿主验证。`review-close`原有交付验证合同也不在本增量中偷偷改变：未close的已编译局部结果照常可保存、显示为候选并接续，不能借新index声称已经推进review head。

不应先把任意目录写成 staging 后一律 rename：现有报告、知识登记或工作包可能包含实际路径。初期采用“唯一最终目录＋最后提交回执＋原子指针”即可实现可见性的原子切换，保留现有相对路径。需要更强的掉电耐久时再对数据文件/目录 fsync 给出明确实现与验证；现有 `os.replace` 不能代替所有耐久性证明。

### 并发与恢复边界

| 场景 | 最小行为 |
|---|---|
| 同任务两次编制并行 | 各写独立 output；短锁＋expected generation/head 只让一个推进选中指针，另一个保留为候选并返回冲突；不长时间锁住模型阅读 |
| 同文档两个任务或两个 producer | task/work 与binding独立，结果都能列举；没有全局“最新完成者胜出”。同producer的序号只在该任务内比较 |
| 同一 SHA 属于两个业务文档 | 工作目录/显式binding包含真实项目、文档、任务；source hash只验证字节，不决定目标 |
| 编译中途失败 | 不存在完成回执，旧已提交结果不动；同一逻辑操作的新尝试分配新目录。若要从残留结果恢复，必须由程序重验并另记恢复回执，不能补造原成功事件 |
| 编译已提交、索引更新前失败 | 扫描**已知任务目录中的提交回执**重建索引，验证其依赖；不扫描任意JSON/mtime推断完成 |
| 应用保存成功、客户端没收到响应 | 用原幂等内容/command和expected版本重试或查询；已有plugin内容digest与head可复用，不新建第二份模型任务 |
| review新版本到达、旧反馈待处理 | 旧反馈保持原绑定，新任务context显示需要重核/未适用；不把取回、修订报告和接受合并 |
| 服务或会话结束 | 已提交的工件、源证据、review与应用版本继续可达；运行状态与成果状态独立。恢复先核验版本与能力包，不自动把旧任务切到新安装包 |

单机任务目录可以复用已有 OS 文件锁；原生正式 working head/交付继续用现有 PostgreSQL CAS/事务。没有必要为本地目录元数据改造一套分布式协调服务。将来多主机共享同一写入空间时，再根据实际部署选择事务 owner；不能把本机 flock 外推为已经支持所有分布式存储。

## 代码字典、manifest 和 DB 的取舍

三者承担不同责任，不是三选一：

- **代码类型字典：本轮新增。** 小而稳定，描述现有工件与依赖、允许的组合和读取角色；不吸收任务业务数据，不自动授予producer写权限。核心可放入现有共享 runtime，使 portable/native 导入同一份；打包清单需把它纳入冻结包。
- **每任务 manifest/index：本轮新增或适配。** 承担可发现入口、确切成果引用和本地操作提交；程序维护，Agent 可读。保留 `workspace-manifest.json` 为 review owner，不重写既有格式；新 index引用它，跨索引权威关系明确。project/document index是可重建的目录，不承载工程结论。
- **新通用 DB 表：本轮不加。** 通过 native/plugin/read-only legacy adapter 列举已有权威结果即可。需要新 API 时先做薄聚合读取，不要求所有旧版本先回填新表。只有实际出现跨主机可靠提交、统一保留/回收政策、跨任务检索规模等无法由现有 owner承担的需求，再按一个明确类型/owner增量迁移。

统一读取引用可先用带owner的既有标识：plugin为`session_id + revision_id`，portable为`review root + review_sha256`或已提交operation，native阶段为`task_id + stage + artifact_sha256`，native工作图谱为实际成功job/object引用及Map/baseline SHA，交付为文档内delivery revision。索引组合这些引用并校验依赖即可；不需要为每个旧对象再发一个全局result ID。只有某个结果集合实际组合了独立可选工件，才保存该组合的固定成员引用，不能按各类型“最新”动态拼装。

现有 extraction object staging/CAS/GC是可以参考的实现，但其 job ownership、引用表和回收条件与 task workspace不完全相同。不能只因为已有GC就把全部任务工件塞进旧job引用关系，更不能让同一对象在新旧回收系统中各有一套互相不知道的 owner。

## 首个可执行增量与验收

建议以**一个项目、两份文档、其中同一原件再开一个独立任务**作为目录验证，而非仅验证一份PDF的native/plugin切换。后者已有P2基础，无法暴露同SHA绑定和多任务发现问题。

下面的新入口名称仅是设计提议，当前 `reqpilot catalog` 尚无这些命令；实现后才可写入skill为可用能力。

| 顺序 | 有界改动 | 复用/新增边界 |
|---|---|---|
| 1 | 建小型工件字典；定义任务context/index与提交回执 | 复用现有schema、hash与validator；不扩业务对象，不迁DB |
| 2 | 增加任务初始化/状态/执行包装入口，可暂命名 `workspace init/status/run` | 新增CLI路由和薄workspace模块；`init`返回真实task根，`status`复用native_task_brief，`run`调用现有compiler并自动分配路径/填写绑定。名称/参数以实际实现后catalog为准 |
| 3 | 修复task绑定贯通与阶段引用一致性 | `workbench_tools`前后hooks与显式workbench/pull消费同一任务binding；native publish/read只将同Map的ITEMS呈为当前阶段。这里处理下节已确认缺口 |
| 4 | 接入编译后提交、source-only/Map-only与review指针 | 先覆盖capture/reading、Map、Items、findings报告及已有review/交付；保留完整证据依赖。导出不是保存局部成果的前置 |
| 5 | 应用增加文档下已存任务/成果的列举与显式选择 | 读既有plugin session head、native任务/working/delivery owner，返回带来源的refs与能力；UI沿用DocumentWorkSurface/reader，不重造外壳，不启动模型 |
| 6 | 原有portable单目录与native旧根兼容验证 | 旧命令带显式--output-dir继续可用；旧review/版本字节不变。新索引可只读导入旧目录引用，不能猜测缺失目标/状态；无需一次回填全部历史 |

验收必须看到实际行为：

1. 新会话只收到 task root 和真实委托，即可找到原件、目标、当前范围、已保存结果、未处理/未知范围、待确认事项及工作台链接；无需开发者解释路径，也不全文预读历史输出。
2. 两文档互不串号/串反馈；相同SHA分别绑定不同文档或任务后，编译前确认检查、编译后同步、pull均落在正确session；远端当前版变化返回明确冲突。
3. Map-only保存后终止会话，新会话可打开同版Map和来源/审计证据，继续或等待已要求的确认；无需伪造baseline，也无需先导出Excel。
4. Items仅处理部分范围时，台账/简报显示实际已保存范围和剩余；KEEP 1/13仍显示剩余12；字典不把可选分析缺失算作整体失败。
5. 已提交、候选、待同步、应用可读、来源核查、人工接受可分别核对；源文件/收据缺失或hash不符时不给“全部完成”。
6. 同任务并行提交及三个中断点——bundle写一半、提交回执已写但index未更、远端已保存但客户端未回执——均保留旧有效成果，恢复不重复发布、不覆盖别任务、不重新发起模型。
7. 从旧Map A生成的Items不能被当作Map B的当前阶段；历史A/Items仍可显式查看，最终同版导出继续核对source/Map/baseline/snapshot，而非只对数量。
8. 迁移/复制一个任务目录后，已声明可离线恢复的阅读、Map重放、review和交付依赖均可达；仅有应用引用的对象明确标需应用访问，不能声称完全离线。
9. native与plugin可从统一文档入口显式选择；plugin外部状态未知时保留协作和成果，不挂载native执行控制器。旧客户版本、人工决定、现有v1/v2/v3与链接不被重签。
10. 不以目录自动化为理由增加模型调用、逐项审批或固定阅读步骤；找得到额外授权页、允许修订上游Map，程序继续承担普通Excel导出。

测试先覆盖上述身份/提交/恢复合同，再做现有两种adapter的聚焦回归和一次真实UI流程；无需为目录结构先重跑Sol/Luna提取能力对照。没有真实中断、应用或UI证据的部分要保留“未验证”。

## 本次复评发现的事实错误、缺口和风险

下列程度表示对本建议的影响，不代表已发生客户数据损坏。

| 编号/程度 | 已确认事实与复现依据 | 边界与建议 |
|---|---|---|
| A01 高：方案偏题 | Sol原文“最小可实施增量”要求family与多张通用表，“更简单替代方案”优先不创建长期实体目录；当前已有任务目录、简报、review和应用版本owner | 这是实施范围/优先级问题，不是代码缺陷。改为实体任务工作区先交付，DB后置 |
| A02 中：绑定没有完整任务作用域 | `workbench_tools.py:47`只用cwd＋source SHA；`_target:77–87`拒绝同默认binding换业务目标；`sync_after_operation:189–202`和`before_operation:206–231`固定找默认binding，而显式`--binding-file`仅workbench/pull接受。独立内存探针及主任务实际harness均复现：custom绑定成功并待Map确认，但before返回None、after不sync；换到reading子目录也使默认binding检查失效 | 这是当前确认点/自动同步发现缺口，同SHA多任务进一步放大它；优先固定任务根解析及binding完整传播。服务端仍会拒绝未确认Map的Items同步（`plugin_workbench.py:246–247`），未证明客户成果被误写；不能删掉target校验 |
| A03 中：当前阶段引用可不一致 | `source_document_stages.py:23–39`只在发布MAP时清除不匹配ITEMS；发布ITEMS时不比对当前MAP。关闭确认条件下可写入旧Map的Items；`source_document_loop_jobs.py:631–648`的stage GET只验source/本阶段hash | 独立内存探针确认索引及GET；主任务另以实际编译的8 SRC合成fixture复现publish/read接受不同Map SHA。`native_task_brief`已正确过滤旧Items；B只产生不同有效摘要，未证明语义差异，**未证明最终Matrix/Excel混版**。补当前阶段关系校验，历史成果保留 |
| A04 中：不是所有保存都已原子成组 | `artifact_tools.py:23–39`和compile/deliver各顺序写文件，无整组完成标记；旧review head原子替换不能保护此前每个compiler输出目录 | 已确认实现属性；崩溃造成半目录的影响是风险推断，本复评未做磁盘故障注入。新增提交回执，不能从文件存在宣布成功 |
| A05 中：文档多任务发现不完整 | `plugin_workbench.py:275–283`只提供latest session摘要；`_remember_latest:82–83`是导航提示。已有具体session/版本可读，不等于能从项目目录完整列出所有任务 | 增加受控owner列举/引用聚合，不能再用latest替代任务目录；无需自动合并或新结果库 |
| A06 中：工件文件名不能代替依赖集合 | `compile-items:585–587`、`revision_tools.OPTIONAL_FILES/open_review`只复制部分Map文件；`audit-map:217–235`需要原Map输出中的decisions/source-ir/source副本 | 这是当前命令/目录的兼容边界，不等于review整体失效。应保留并索引原Map编制目录及前驱证据；不要把所有“decision输入”都当可清理scratch |
| A07 中：findings状态仍有限 | `source-findings/v1`已存在，原报告主体“schema/保存待实现”已过时；当前是限定扫描与只读投影，尚无通用resolve。report ID不等于可跨Map继承的处置身份 | 更新评估当前状态；新索引显示扫描范围/版本和未核查，不宣称通用闭环已完成 |
| A08 低：统一UI已有首切片但不是全部迁移 | 代码共用外壳、协作和plugin查看版；native stage、legacy Set、多个任务选择仍有不同owner/入口 | 保留P2成果，按具体残余能力接adapter；不把“仍需适配”夸大为整个UI不存在。此次未重跑实际UI |

针对A03，旧Map对应的Items本身仍可以是有效成果。修复应保留它为历史/候选，显式说明配套Map和“当前结果集合未就绪”；不能为让当前索引整齐而丢弃它，或把有效局部保存一概改成失败。

W09“tuple索引误报”继续撤回。`revision_tools.py`相关表达确为 `dict.get(key, default)`，不列为缺陷，也不基于它扩测试或修改实现。

### 可重现的只读绑定探针

以下命令仅在内存替换传输依赖，不访问应用、不写客户/运行文件；结果为 `auto_sync_called=false`。它证明hook忽略该显式参数，不能单独证明真实网络效果。

```bash
PYTHONDONTWRITEBYTECODE=1 prototype/.venv/bin/python -B - <<'PY'
import sys
from pathlib import Path
from argparse import Namespace
from unittest.mock import patch, MagicMock
sys.path.insert(0, str(Path('plugins/reqpilot/scripts').resolve()))
import workbench_tools as t
default = MagicMock()
default.is_file.return_value = False
args = Namespace(command='compile-map', output_dir=Path('/synthetic/map'),
                 review_dir=None, binding_file=Path('/synthetic/custom-binding.json'))
with patch.object(t, 'source_and_artifacts', return_value=('a'*64, {}, None)), \
     patch.object(t, 'binding_path', return_value=default), \
     patch.object(t, 'sync_workbench') as sync:
    t.sync_after_operation(args, {'status': 'COMPILED'})
    print({'auto_sync_called': sync.called})
PY
```

### 隔离验证回执与证据程度

主任务针对本复评发现另做了两项隔离验证，回执位于私有 `prototype/runtime-data/document-folder-astra-review-20260912/`：

- `binding-hook-probe.json`：真实生产hooks＋既有 `WorkbenchTransportTests` in-process API harness，custom binding和切换cwd两场景均复现；没有复写hook分支、触生产或改源码。
- `stage-pair-probe.json`：使用既有合成PDF/真实compiler形成的8 SRC基线，先发布不同有效SHA的Map B，再发布基于Map A的Items。真实publish/read接受，简报过滤为仅MAP。临时真实文件已清理；没有UI或最终Excel测试，不能扩大为语义混版证据。

本复评也独立复跑了纯内存stage探针：用schema/hash有效的EMPTY baseline和合成Map指针直接执行 `publish_stage/read_stage`，抽取真实stage API函数体读取，返回DRAFT；`build_task_brief`返回MAP_SAVED且只列MAP。仅替换文件存取和外层应用查找，不修改阶段判断逻辑；没有文件写入。这是阶段机制验证，实际非空基线行为由上述harness补强。复现顺序是“MAP B指针→compile-items产出的A基线指针→stage ITEMS GET→build_task_brief”，各验证入口和代码位置见A03。

复评输入冻结清单为同根 `input-state.json`（2026-09-12 04:38:58 UTC）；Sol原评估SHA为 `5c5fb91e595b621407019db149ed55007e0f5679d03202c5a0a7b372da0520c6`，原字节副本在 `before/docs/需求提取/文档任务文件夹与工件约定评估.md`。

本复评完整读取了指定评估、两份实施记录、主专项和唯一计划，并核对上述代码；没有新建业务测试文件或运行新的文档提取模型对照。提交前检查两份文档的内容、链接和差异。原执行测试数与安装回执只作既有证据，不冒充本复评重跑结果。

## PPT Master 参考边界

用户尚未确认具体同名项目。本复评重新读取了 Sol 选用的候选 [hugohe3/ppt-master 入门文档](https://github.com/hugohe3/ppt-master/blob/main/docs/getting-started.md#set-up-the-installation-and-working-folders)：它确实区分安装目录、可持续写入的工作目录和初始化后返回准确路径的 active project；[项目入口](https://github.com/hugohe3/ppt-master/blob/main/AGENTS.md#command-quick-reference)列出了程序初始化、导入与验证入口。值得借鉴的是**Agent拿到具体工作根、材料和产物有约定、工具承担机械工作**。这是对本项目方案的启发，不是采用其PPT阶段、确认制度、存储设计或任何同名项目代码的依据。

当前建议是让一次真实目录任务证明这些收益，再决定是否需要更广的数据层收敛。文件夹、Agent自由度与可靠注册可以同时存在；本轮应把它们接在现有代码上，而不是先把用户建议翻译成平台迁移。
