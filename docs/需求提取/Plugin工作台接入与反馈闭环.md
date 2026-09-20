# Plugin 工作台接入与反馈闭环

更新：2026-09-12。PDF来源绑定、版本、确认/反馈与同版Excel链路已有真实联调。**用户随后指出界面用错：首轮自建了插件主界面，仅复用少量组件，不符合沿用现有工作台的要求。** 已撤掉自制清单与专用布局，改为向原有`NativeStructureReferenceDock`提供版本数据，继续使用既有结构树、`SourceLedgerTable`、`SourceItemLens`和PDF精确定位。程序与界面回查不等于用户接受。

本次根因是把“插件工件与应用工作版分别存储”误解为“插件需要单独主界面”，又以传输测试和组件复用代替完整使用方式核对。存储隔离应落在读写适配器，不能因此换掉用户已经使用的交互界面。

## 已可操作的范围

- 插件以实际项目、文档、原件SHA绑定工作台，保存不可变草稿版本；文档列表可重新找到插件草稿。
- 编译保存Map/条目后自动同步；明确要求结构确认时，未确认不会创建后续条目工件。已确认的同一Map可复用确认，不逐项重批。
- 前端先核对结构，随后可查看完整原文、SRC/REQ、分类、派生、表格、原图、图片释读和明确关联的条件。
- 用户反馈绑定正在核对的来源、版本和对象。插件取回后，匹配基线的反馈只导入一次；修订完成后回到工作台，标明“插件已提交修订，待核对”。
- 新版本到达时保留用户正在看的旧版。意见文字与原对象不被自动改绑；需要时由用户明确改为当前范围。
- 从当前核对版导出Excel，调用同一快照的内部程序。历史版本仍可回查和导出。

**首轮支持PDF原文/分类、图像释读、拆分和分析策略。** 已含符合性分析或人工意见文件的review会明确拒绝，不能删掉这些文件强行导入；其内部证据/人工来源的完整传输另行接入。Office、自动唤醒已经结束的外部Codex对话、冷启动模型全流程泛化未在本轮完成。工作台保存的是明确标记的插件阶段草稿，不等同于替换应用工作版或人工接受。

## Agent 的实际入口

先在ReqPilotX选定项目并添加该原件，沿用当前文档URL。CLI自动读取URL中的项目/文档身份；不要求工程师手抄ID。

```text
<python> <plugin>/scripts/reqpilot.py workbench --source <原件.pdf> --url "<当前文档工作台URL>" --open
```

连接后，`compile-map`、`compile-items`、`compile-images`、`review-open`、`review-close`在成功保存后自动同步到默认绑定。网络失败保留本地工件并报告未同步；已保存且匹配该Map的确认可供临时断网时继续本地工作。未确认的新Map仍需实际确认。

已有成果可显式同步：

```text
<python> <plugin>/scripts/reqpilot.py workbench --map-dir <已保存Map目录> --url "<文档URL>"
<python> <plugin>/scripts/reqpilot.py workbench --baseline-dir <条目目录>
<python> <plugin>/scripts/reqpilot.py workbench --review-dir <当前review目录>
<python> <plugin>/scripts/reqpilot.py workbench-pull --review-dir <当前review目录>
```

`--map-dir`、`--baseline-dir`、`--review-dir`互斥。初次直接同步完整review但尚需结构确认时，只发布其Map，返回`pending_items_sync`；确认后再同步已保存条目。首次连接可用`--no-map-confirmation`表达已有明确授权的免确认分支，不靠此参数绕过用户要求。

绑定和收件箱在`runtime-data/workbench-bindings/<source SHA>.json`及相邻`-inbox.json`。尚无review时用`workbench-pull --source <原件>`取回结构反馈。存在review时，匹配基线的实际用户反馈进入原有`review-feedback`链；其余版本的反馈保留在收件箱，由Agent回源判断后修订，不静默套用。取回命令不产生新模型回合，外部Agent继续使用自身对话；已结束的对话需后来继续。

## 数据与接口

新增模块：`prototype/app/application/agent_workflow/plugin_workbench.py`。草稿按项目/文档作用域保存于配置的ObjectRoot下`runtime/plugin-workbench/`，每个会话保留不可变版本、来源、结构确认、反馈与处理报告。版本更新采用乐观并发和进程锁；重复内容同步不膨胀历史，回到旧内容也形成新版本，不改写旧版本。

请求只接收支持的JSON工件，重用DocumentMap、baseline、receipt、annotation、decomposition及delivery校验。核对应用登记原件的实际SHA、PDF类型和页范围；不接收任意服务端文件路径，不执行上传脚本。来源/完整性检查不等于语义通过或人工接受。

以下路径均位于`/api/projects/{project_id}/sor-documents/{document_id}/agent-structure/delivery`：

| 方法与路径 | 用途 |
|---|---|
| POST `/plugin-workbench` | 绑定来源或发布工件，含会话、来源、期望前版、支持的artifacts与可选反馈处理结果 |
| GET `/plugin-workbench/latest` | 返回当前文档的最近插件入口，供文档台账发现 |
| GET `/plugin-workbench/{session_id}` | 当前版本、历史、结构确认和反馈状态 |
| GET `/plugin-workbench/{session_id}/snapshot?revision=...` | 指定版本的共享Matrix快照 |
| GET `/plugin-workbench/{session_id}/stages/{MAP|ITEMS}?revision=...` | 带版本的阶段分页；原生阶段组件复用此接口 |
| GET `/plugin-workbench/{session_id}/reader?revision=...` | 将同一版本投影到现有结构树、条目台账、表格、审阅索引和派生读取合同 |
| GET `/plugin-workbench/{session_id}/records/{source_record_id}?revision=...` | 经共用原文详情投影读取指定条目、原图、释读与明确关联条件 |
| POST `/plugin-workbench/{session_id}/confirm-map` | 只确认指定版本的结构，不启动原生模型 |
| POST `/plugin-workbench/{session_id}/feedback` | 保存具体版本/对象的实际意见，重试幂等 |
| GET `/plugin-workbench/{session_id}/export?revision=...` | 按指定快照程序导出，页首展示登记的原文件名，不使用对象存储的物化文件名；响应携带版本与快照标识 |

前端`PluginWorkbench`现只承担版本/反馈的数据适配与现有文档外壳，主体由原工作台组件渲染；不再维护自己的原文表格、PDF iframe布局或独立样式。它挂在原有`/workspace`的`pluginSessionId`路由下；切换项目/文档或离开文档任务时不会粘住旧会话。原生任务/工作版维持原路径，插件草稿有独立入口。此处仅增加传输与交互适配，没有另建语义模型或模型调度工作流。

## 真实联调证据

私有根：`prototype/runtime-data/plugin-workbench-implementation-20260912/`。使用前轮已有五页**合成SOR**与冻结工件测试承接，不称新模型阅读成绩；没有修改客户基线。

- 在隔离PostgreSQL `55472`与API `18080`运行真实应用，worker未启动、Agent runtime为none。`services.json`保存本次自有进程/存储；没有启动旧生产集群，旧集群的过期PID指向另一个程序也未被终止。
- 通过真实项目/上传API注册合成文件，再用CLI同步。v1先呈现22节点Map；真实浏览器点击结构确认后，CLI取回并同步v2：17 SRC、13 REQ、2 Snn。
- 在浏览器选中V-01，提交“补充验证主题，保留完整原文/分类/跨页内容，不新增需求”的合成联调意见。CLI取回一次，沿原有review-revise→deliver→review-close形成v3并自动回传；前端显示实际新增“验证”主题、对应反馈和待核对状态，旧v2保留。
- 对2.4验证跨页完整文字；对V-01核对两页方法、条件与判据；对2.6核对原图与单独的Agent释读；对2.2核对定义及12V/23C的实际关联条件。项目文档列表可以重新找到v3。
- 从浏览器点击指定版本的Excel链接并观察下载事件；程序另读同一接口核对文件。应用快照与本地已关闭review快照相同，实际Excel的快照元数据一致；48项来源/顺序/跨页/派生/图片及版本检查通过，V-01的Excel主题为“试验验证”。
- 另一次材料齐备的真实CLI探针证明：连接后compile-map自动发布，未确认时compile-items不创建输出，确认后条目自动发布。探针结束将文档默认插件入口恢复到经过反馈修订的v3。

主要回执：`first-sync.json`、`workbench-pull-after-confirm.json`、`feedback-pulled.json`、`feedback-revision.json`、`feedback-closed.json`、`same-version-delivery.json`、`ui-delivery-verification.json`、`automatic-stage-verification.json`。本轮结构确认/意见由测试操作者在合成环境执行，不是客户工程师接受。

## 联调中修掉的显示问题

复用组件不自动保证输入正确。最初把所有逻辑表传入详情，导致2.6接线条款带入无关BOM；现只传明确归属的表格，条件按真实引用展示。图片释读字段也已接入详情，不再仅有原图。旧表格组件把已分类条目标成“未做语义判断”、把完整保留事项写成“尚未细化”，已改为读取实际分类并中性说明保留整体。均保留聚焦回归。

首轮为了自制界面增加的滚动/固定栏样式已移除。现在恢复既有列宽拖动、树形导航、内容/PDF联动、表格审阅/结构与原文切换和条目内反馈。插件条目/结构未提交意见按来源、对象及传输版本保存，版本变化不静默改绑；反馈复用现有编辑器送往插件端，禁止误入原生模型调度。明确关联条件也补入共用详情投影，避免旧组件接入时丢失已有annotation关系。

本次在隔离环境回查同一v3，结构22、SRC17、REQ13及2个派生保持；原图与释读、PDF框标、跨页表及历史反馈继续核对。原客户基线未改。首轮自制页面的浏览器/Excel证据保留为传输证据，不能继续称为正确界面已通过。

## 验证和发布边界

界面纠正这一轮：76项前端聚焦回归、51项后端读取/反馈/来源/导出相关回归及TypeScript/Vite构建通过。实际API逐条读取17个SRC，确认关联条件、图片释读、跨页表和旧反馈仍在；v3 Matrix快照与纠正前逐字段一致。实际浏览器回查了既有三栏工作台、图片框标、跨页试验表及“插件已提交修订·待核对”反馈。回执为`ui-correction-verification.json`，两侧测试日志为`ui-correction-*-tests.txt`。此前首轮测试数如下保留，不能替代本轮界面要求。

66项插件测试、17项API/阶段/来源顺序与图片绑定回归、30项前端聚焦测试通过，TypeScript/Vite构建通过；静态UI检查为空。构建仍有既有大bundle提示，不把它当成语义或交互失败。此轮未跑新的Sol/Luna提取任务，未证明所有PDF、模型稳定性、内部证据传输或人工接受。

新插件`0.3.0+codex.20260911192437`已安装启用，148文件源/包/缓存一致，包SHA `5e97c755c7927cb7f46222fadbdb297d7de717536608eb30a7256c201bc3c6b8`；安装回执位于`release/installation-verification.json`，旧包与旧客户成果保留。后续重点为拆分处置范围/KEEP理由持久化、输入机械负担及带本次真实界面链路的模型复测，仍按[唯一计划](开发计划.md)推进。
