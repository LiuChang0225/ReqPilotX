# Pair 单文档阶段编排与注意力聚焦

日期：2026-11-01。审计基线：ReqPilotX `97afead`；候选为 `codex/reqpilotx-workflow-ux-20261101`。 关联规格：[GitHub #23](https://github.com/LiuChang0225/ReqPilotX/issues/23)。本规格区分已观察代码、待实施增量和未实跑效果。真实 Pair 集成工作区另有知识绑定/launcher WIP，本轮不修改该工作区。

## 目标与判断

一个客户文档保持一个已绑定的 DSH Session、一个业务任务与同一版本链。Agent 依当前阶段切换专业职责并按需加载已有角色技能。先在现有任务读取回包里增加紧凑、可行动的阶段聚焦信息，不新建 Agent 平台，也不为每个 UI 页签新建 Session。

“不同职责”不等于“已经有多个独立 Agent”。同一模型会话中的角色提示不会清空历史上下文，也不能证明专长或质量提升。后续确需独立上下文的视觉复核、矛盾审查等子任务时，先验证实际 DSH 子任务接口与回传合同；本增量不臆造该能力或默认创建子 Agent。

## 实际状态与缺口

| 领域 | 代码证据 | 判断与缺口 |
|---|---|---|
| 固定工作流 | `packages/engine/src/reqpilotx/application/agent_workflow/fixed_workflow.py:22`、`:224` | 已有 PREPARATION、SOURCE_MAP、ITEMS_CLASSIFICATION、EVIDENCE_ANALYSIS、SELF_CHECK、DELIVERABLE 六阶段；完成只认保存工件，不能用模型文字或 Session idle 推进 |
| 当前任务出口 | `plugins/reqpilot/scripts/task_service.py:409` | open/inspect 已返回 source、goal、revision、selected、workflow、pending_decomposition 和可用读取句柄；尚无统一的职责、输入基线、退出证据和下一动作摘要 |
| 角色方法 | `agent-assets/current/plugin-skills/reqpilot/SKILL.md` 的“按问题取得方法” | map、source-items、review、verify 已分工且按需加载；不需重写同义角色体系。但阶段事实与角色入口之间还靠模型自行联系 |
| 初次派发与恢复 | `plugins/reqpilot/scripts/host_launch.py:109`、`:128`；`integrations/pair/host/src/task-launch.js:322` | 既有任务先 open/resume，Host 沿绑定 Session 调用 prompt 并以 durable request ID 修复丢失确认；当前启动说明仍把整个流程交给模型自行选择下一职责 |
| 工具范围 | `plugins/reqpilot/scripts/mcp_server.py:20`、`:29` | 真实 typed 工具已覆盖准备/读源/Map/items/review/分析/自查/交付；允许任意阶段回读相关原文。聚焦只能推荐工具，不能假造权限或限制调查世界 |
| 版本与失效 | `packages/engine/src/reqpilotx/application/agent_workflow/task_workspace.py:901`、`:959` | inspect 读取当前有效工件并重投影；Map/Items 及分析的版本关系已有校验。新增聚焦信息应跟随当前投影重建，不存第二套可写状态 |
| 分类状态 | `fixed_workflow.py:268`、`:298`；`packages/workbench-ui/src/shared/api/contracts.ts:1447` 的 SourceRequirementnessV1 | 原文条目与需求识别当前联合编制/保存；SourceItem 只有 REQUIREMENT/NON_REQUIREMENT。不能把 items.count 当独立识别进度，也不能把缺失分类当非需求 |
| 知识分析 | `plugins/reqpilot/scripts/task_service.py:569`；`fixed_workflow.py:303` | 已要求当前条目、冻结知识绑定和真实分析记录；选择资料或声明策略不等于分析完成。mode NONE 跳过分析，仍自查来源并交付 |

当前运行集成目录在审计时另有 `fixed_workflow.py`、`knowledge_binding.py`、`pair_evidence.py` 和 Host launcher/knowledge 的未提交增量。本轮接缝放在新独立 helper 与 `task_summary` 的附加输出，避免覆盖这些活动实现。

## 用户视图与执行阶段

| 用户视图 | 权威执行事实 | 专业职责 |
|---|---|---|
| 文档结构 | SOURCE_MAP | 来源阅读与结构整理：正文/附件、章节、父正文、表图所属与原序 |
| 原文条目表 | ITEMS_CLASSIFICATION | 完整来源范围、条目边界与上下文保真 |
| 需求清单 | 同一个 ITEMS_CLASSIFICATION 保存事实 | 判断是否需求及类别；仅在授权范围建立工程派生 |
| AI 分析清单 | EVIDENCE_ANALYSIS | 基于冻结知识对明确分析单元给出建议、适用条件与缺口 |

PREPARATION 为输入准备，SELF_CHECK/DELIVERABLE 为自查和交付状态。四个用户视图是同一对象与版本的阅读投影，不制造四个独立执行完成事件。需要“识别前条目草稿”独立保存时，必须另行定义真实 draft 合同，不能用 UI 假进度补齐。UI 可直接使用现有 workflow，不依赖本次 Agent 聚焦包才能展示阶段。

## 增量合同：stage_focus

`task_open`/`task_inspect` 及原本调用 `task_summary` 的成功回包附加可选 `stage_focus`；没有固定工作流的旧任务保持原响应含义，不强行加入阶段。

| 字段 | 定义 |
|---|---|
| schema | `reqpilot-stage-focus/v1` |
| authority | 固定 `GUIDANCE_ONLY`；不是授权、成果或验收证据 |
| mode | WORK、WAIT、RECOVER、COMPLETE |
| stage_id | 沿用既有六阶段 ID；不引入新的业务阶段 |
| role_id / objective | 当前应专注的已存在角色与简短工作目标 |
| basis | task_revision、source_sha256；已有时附 map_sha256、items_sha256、review_sha256、knowledge_binding_sha256；缺失不伪造 |
| recommended_tools | 现有 MCP/typed 业务工具或已挂载的 `reqpilot_knowledge` 证据工具名称，只作建议 |
| skill | 已打包角色技能 ID、包内路径及 `relative_to=FROZEN_CAPABILITY_ROOT`；消费者在冻结能力包根解析，bootstrap 工作区对应 `.agents/skills/<id>`，不从任务目录拼路径 |
| read_requests | 可直接验证的现有读取请求/句柄；保留其版本，不编造 scope/page/ID |
| exit_evidence | 当前职责完成需要的真实保存工件/检查依据简述，不由 Agent 自报完成 |
| remaining_reasons | 现有工作流或操作恢复原因；有界输出 |
| next_action | 一个现有且可验证、能推进真实阶段的工具请求；TaskService 请求用其 typed 模型验证，外部 Host 读请求用其自身接口验证，二者不混用；未知参数优先 task_describe，不拼凑写入决定。COMPLETE 为 `null`，表示没有后续必需工具 |

聚焦包由已校验 state 纯投影，不执行模型、不写文件、不改变 ready_to_review，也不接受 Agent 上传字段覆盖。Source、Map、Items、review 或知识绑定变化后，下一次 inspect 必须显示新 basis，旧 focus 不成为新写入的 CAS 凭据。

优先级：未知/待恢复的操作先 RECOVER；已声明且尚未满足的 Map 确认先 WAIT；其余根据真实 next_stage 进入 WORK；只有既有 ready_to_review 成立才 COMPLETE。COMPLETE 可停止且不安排额外程序检查，也不构成人工接受。终态失败历史本身不阻止已成功后继，缺知识或 WAIT 都不自动创建审批。缺少依据时沿现有 SKIPPED 分支前进。SELF_CHECK 的下一动作按真实状态选择：缺 portable review 时 `review_prepare`；当前 basis 匹配的 workflow_check 已有 `delivery_status=NOT_REGISTERED` 时 `task_deliver`；其它情况沿用 `task_check`。ITEMS_CLASSIFICATION 出现 `SCOPED_DERIVATION_REMAINING` 时聚焦现有 review 技能和 `decomposition_apply`，不把未决派生说成普通 items 补写。

## Agent 消费方式

canonical `reqpilot` 主技能在恢复任务后读取 `stage_focus`：按当前 role/skill 与版本绑定读取请求工作，只加载当前需要的方法；`skill.path` 只在 `skill.relative_to` 的冻结能力包根下解析，bootstrap 工作区对应 `.agents/skills/<id>`，不从任务目录拼路径。保持完整原文与相关上下文可调查。提交有意义的一批成果后重新 inspect，依新的工件事实更新职责。修改问题跨阶段时先回读关联原文并走现有局部修订合同。

绑定感知：`EVIDENCE_ANALYSIS` 有冻结的 PAIR 项目/团队或 adopted knowledge 绑定时，`recommended_tools` 指向实际挂载的 Host 证据读取与 `knowledge_analysis_save`，`next_action` 是对应接口的有效读取请求；普通 `analysis_save` 仅保留给无宿主绑定的 standalone 路径。动态指引不验证或伪造外部 Host 工具，也不自造 attestation、改绑定/守卫或降级签名。绑定非 CURRENT 或读取失败时保持未决。

聚焦包不是要求每个工具调用后都 inspect；仅阶段保存、版本冲突、恢复未知操作或准备交付时刷新。 COMPLETE 只描述已保存版本；用户的新问题、反馈和修订优先，继续原 task/session 的局部修订，不以旧 ready 提前结束新请求。后续阶段的详细方法按需读取，不把全套方法、全量原文和全量知识反复塞入主会话。WAIT 只遵守已有确认且不新增审批，RECOVER 遵守原有恢复合同；COMPLETE 表示当前版本可审阅、可以停止，`next_action` 为空且不代表人工接受。

角色不改变业务边界：原文包含与工程派生分开；提取阶段不拿内部知识改写客户原文；分析阶段只引用当前任务冻结且适用的内部证据；一个阶段可保存部分成果并继续其它可用范围。

## 已核对的 DSH 子任务能力与实际接入缺口

只读核查 PAIR 迁移检出 `fc2710a2f4` 的公开文档与实现，未修改该仓库或启动真实子任务。

- `packages/bundle/web-app/presets/standard.patch.yml:83` 已声明 `subagent`（spawn）、`subagent_fork`（fork）及消息/控制工具。`packages/subagent/tool-subagent/src/index.ts:213` 的模型工具立即返回 child ID，由运行时随后回传完成通知，不同步等待所有中间步骤。
- `packages/subagent/subagent-spawn-in-process/src/index.ts:40` 明确 `inheritsParentContext=false`：独立 Session、独立上下文；支持独立 preset、模型选项、结构化输出与深度上限。persona/工具组成由 preset 决定，不是给任意 toolFilter 字符串就生效。
- `packages/subagent/subagent-fork-in-process/src/index.ts:45` 只继承截至最后一个 `turn/end` 的已完成前缀，不含当前执行中的 turn；fork 不支持独立 preset，不能用来清掉父会话历史。
- `docs/subsystems/subagent.md` 与 `packages/subagent/subagent/src/types.ts:39` 已有 `startActivation({provider,label,request,delivery})`，返回 `childId`、`result`、`dispose()`。服务消费者可选 `delivery: caller` 并使用受支持的 `outputSchema`；模型工具走 parent 通知。每个本地 child 最多一个活跃 activation，本地 child 可消息续作/恢复；外部执行后端不能普遍假设支持 follow-up。默认深度为 1，实际模型路线与 preset 受宿主能力和配置约束。
- ReqPilotX 当前 `integrations/pair/host/src/task-mcp.js:23` 按新 Agent ID 请求任务 MCP，`task-launch.js:466` 只匹配已登记 `record.sessionId` 才返回业务/知识 binding。尚未看到把父任务授权与冻结知识安全绑定给 child Session 的实现。源码存在 DSH spawn 不等于 ReqPilotX 子会话已能使用同一业务工具和证据；也不能直接把父 Session ID 填给子 Agent。

因此本轮保留单文档主 Session + 阶段聚焦。后续最适合独立 spawn 的是有界视觉复核、跨章节条件冲突检查、知识证据适用性二审：输入只给版本化来源/范围/判断问题，返回带引用的观察、差异或建议，由父任务沿已有验证/CAS 路径统一保存。启用前需实际验证 child 的任务授权、只读来源/知识工具挂载、结果回传、取消和版本漂移；本规格不把这些未来接缝记为已经实现，也不默认派生四个可并发写同一账本的 Agent。

独立子任务的后续验收必须覆盖：spawn 的输入自足且 fork 不继承执行中的 turn；子 Session 的业务 MCP 与知识 caller/版本明确绑定；取消后不接受该 activation 的晚到写入；父任务基线变化时旧 packet 结果通过现有 CAS 拒绝或显式重核；正确子结果仍只由原业务任务保存一次。代码和 standard preset 声明证明接口存在，本次没有检查运行中 Agent 的工具列表或启动子任务，不能据此宣称实际安装实例已启用、已经跑通或改善了语义质量。

## 实施范围

1. 新增纯 helper `plugins/reqpilot/scripts/task_stage_focus.py`，接入 `task_service.task_summary`；不修改六阶段定义、领域 artifact schema、Host launcher 或知识权限/attestation。
2. 在 MCP open/inspect 描述和 canonical 主技能增加短消费规则；现有角色技能内容继续按需读取。通过 asset registry 同步发行投影，不直接编辑投影。
3. 增加实际 TaskService/fixture 回归：首次任务→Map→Items→从 items 后跟随 `next_action` 到 check/deliver，不手工补跳步；原 confirm/recover/ready 规则保持；旧任务响应兼容。
4. 验收输出体量和公开可调用请求。聚焦包不得含源全文、完整知识正文或隐藏思维链；大小应保持在一份短任务简报的量级。

## 验收与未覆盖边界

- WORK、WAIT、RECOVER、COMPLETE 由保存事实触发，未知或不支持状态不伪装完成；旧 ready 判定逐项保持。
- 修改 Map 或 Items 后，basis 与请求中的版本更新；旧 focus 不能越过现有写入版本检查。
- 无知识时不建议伪造分析；有冻结知识时不允许仅凭绑定存在标分析完成。
- read_requests/next_action 通过真实请求模型校验，角色路径在打包后存在；待恢复操作使用既有 recover，不重复执行 mutation。
- 原文与需求视图共享 ITEMS_CLASSIFICATION 时，代码/文档明确联合保存，无假独立进度。
- 聚焦输出有界、无需预读所有技能；测试只证明确定性路由和接口可用。未运行真实模型前不宣称降低 token、提高语义质量或多 Agent 专长已验证。

当前状态：审计、规格与本节限定实现已完成，聚焦程序和冻结包验证通过；真实 Pair/模型效果由集成验收单独记录。

主线集成（2026-11-01）：在 `6d4b23a` P0 Host 修复后，与工作台增量 `d87a1c4` 一并合入当前迁移 checkout。canonical 主技能中的 `stage_focus` 消费规则与 `e6b01e4` 的 adopted knowledge reference 指引同时保留；合并后 `current-portable-skills` 树哈希为 `d560daf55ff59b9b9f0676ca04bdae9cf7889ccf7477647dc8e5f29e1fcc2522`，由资产注册器按源树重算并经 sync/check 验证。`stage_focus` 仍为 GUIDANCE_ONLY，不改六阶段、权限、完成判定或宿主运行。

## 实施验证状态（2026-11-01，基线 `97afead`）

已实施：新增 `plugins/reqpilot/scripts/task_stage_focus.py`，由已验证 state 纯投影 `stage_focus`；`task_summary` 仅在存在固定工作流时附加该字段。MCP `task_open`/`task_inspect` 描述和 canonical `reqpilot` 主技能增加短消费规则，经 `asset_registry sync --group current-portable-skills` 更新 `plugins/reqpilot/skills` 投影与该组 catalog hash。未修改六阶段、`fixed_workflow.py`、`task_workspace.py`、Host launcher、知识绑定/attestation/权限、UI 或父开发计划。

返工修正：

- SELF_CHECK 的下一动作不再固定为 `task_check`：缺 portable review 时给 `review_prepare`；当前 basis 匹配的 workflow_check 明确 `delivery_status=NOT_REGISTERED` 时给 `task_deliver`；其它情况才给 `task_check`。真实 fixture 从 items 后只按 `focus.next_action` 执行，实际序列为 `review_prepare → task_check → task_deliver → COMPLETE`。
- COMPLETE 的 `next_action` 为 `null`，`recommended_tools` 仅保留只读入口；语义改为“可以停止、不代表人工接受”，不再制造新的必经检查或审批。WAIT 仍只遵守任务原有的 Map 确认策略。
- `ITEMS_CLASSIFICATION` 的 `SCOPED_DERIVATION_REMAINING` 分支改用现有 `reqpilot-review` 技能，推荐 `decomposition_apply`，下一动作是 `task_describe(action=decomposition)`；普通 items objective 不再声称一次保存全部工程派生。
- `skill` 增加 `relative_to=FROZEN_CAPABILITY_ROOT`；路径仍是包内 `skills/<id>/SKILL.md`，bootstrap 工作区对应 `.agents/skills/<id>`，不从 task_root 拼造路径。
- MCP 回归断言改为公开 action 集合 `set(REQUESTS) - {'evidence_status'}`，并验证公开 schema 不含 `EvidenceStatus`；工具集合与权限未改。

实际执行：

- `prototype/.venv/bin/python scripts/test_python.py plugin --pattern 'test_task_stage_focus.py'`：5/5 通过。包含真实 TaskService `open→prepare→map→items→跟随 focus.next_action→COMPLETE`、真实 WAIT、旧任务无 `stage_focus`、WORK/WAIT/RECOVER/COMPLETE、缺知识与冻结知识、`SCOPED_DERIVATION_REMAINING`、上游 Map/Items 变化、真实 Request 模型校验、无正文泄漏，以及实际 `build_bundle` 后冻结包内 `python -I` 执行 open/inspect。
- 冻结包证据：`build-receipt.json` 的 `plugin_files` 包含 `scripts/task_stage_focus.py`，`skills/reqpilot-source-map/SKILL.md` 等角色文件存在；冻结脚本在临时工作区返回 WORK `stage_focus`，inspect 返回相同 focus，全程不依赖开发 checkout 的 `PYTHONPATH`。
- `... --pattern 'test_fixed_workflow.py'`：9/9 通过；旧 ready 与阶段判定未变。
- `... --pattern 'test_task_service.py'`：7/7 通过。
- `... --pattern 'test_mcp_task_adapter.py'`：9/9 通过；公开集合为 19 项 `set(REQUESTS) - {'evidence_status'}`，`EvidenceStatus` 不出现在公开工具 schema 中。
- `prototype/.venv/bin/python -B agent-assets/scripts/asset_registry.py --repo . check`：通过，37 个 portable skill 文件与投影一致；本组 hash 已更新。

聚焦包序列化体量（UTF-8 JSON）：合成的普通 WORK 包约 948 字节；附加 200k 级来源/知识正文及大历史字段后约 1044 字节，正文未进入输出；真实 fixture 各阶段约 986–1703 字节。输出上限测试为 6000 字节。

未覆盖：未运行真实 PAIR/Host/模型，不声称 token 节省、语义质量提升或多 Agent 专长已验证；冻结知识分支只用有效绑定 fixture 验证路由和 basis，未执行真实知识检索/attestation 分析；真实 Host 下 SELF_CHECK 的 review/check/deliver 连续动作仅有本地 TaskService 与冻结包证据；UI 由另一负责人同候选并行处理，本轮未操作。

独立复核：未发现阻断；另行执行真实 TaskService 推进用例 1 项通过（3.516 秒），确认 GUIDANCE_ONLY 不改权限/ready，WAIT/RECOVER/派生剩余/COMPLETE 与原合同一致。新增消费规则明确新反馈优先于旧 COMPLETE；该模型依从性尚未做真实调用验证。实际命令输出、两轮模型元数据与返工/验收记录保存在本候选忽略目录 `work/workflow-focus-checks.json`、`work/workflow-focus-20261101.json`，含首轮失败及修复后的通过结果，不将失败回执重标。
