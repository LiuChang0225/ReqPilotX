# App Server 自主任务运行入口

更新：2026-09-08。本文记录持久任务宿主的实际实现与验证边界；业务交付顺序仍由[开发计划](开发计划.md)维护。

## 实现边界

`prototype/app/adapters/agents/codex_app_server/native_loop_driver.py` 提供
`LocalCodexAppServerNativeLoopDriver`。它接受目标、同一份可移植 Skill、明确绑定的任务和动态工具宿主，
由 Agent 自行选择读取和修订范围。它不拼装固定来源 packet、不指定分类窗口、不要求最终返回候选 JSON，
也不根据自然语言答案认定业务完成。来源读取、语义工件、校验、原子保存和 Matrix 导出由共享工具负责。

现有 `json_stage_driver.py` 继续承载兼容的结构化阶段调用。新入口与它的区别是：

| 能力 | 新入口行为 |
|---|---|
| 证据调查 | 注册工具按需返回完整文本、表格、当前工件和图片；上下文摘要不限制调查范围 |
| Skill | 显式加载固定版本；同包的 references 由 `reqpilot_skill` 按需读取 |
| 输出 | Agent 调用共用写入、编译、校验、保存、导出工具；终态说明不替代这些工具回执 |
| 反馈 | 活轮次使用 `turn/steer`；等待状态通过同一线程的新轮次继续 |
| 取消 | `turn/interrupt`，读取终态快照验证 `interrupted`，保留成果和历史 |
| 恢复 | 保存真实 thread/turn 身份，先读取服务端状态，再 `thread/resume`；不会因观察超时新开替代线程 |
| 生命周期 | 单轮 App Server 进程可以结束，任务工作区、调用回执及线程保留；不调用 `thread/delete` |

## 应用调用合同

```python
driver.run(
    session_dir=private_runtime_directory,
    goal=authorized_goal,
    skill_paths={"reqpilotx": entry_skill_path},
    skill_bundle_root=plugin_root,
    tool_host=host,
    binding={"project_id": project_id, "document_id": document_id,
             "source_sha256": source_sha256, "task_id": task_id},
    cancellation_requested=should_interrupt,
    steer_requested=next_feedback,  # (command, text) 或 None
    steer_delivered=acknowledge_feedback,
    steer_failed=record_failed_delivery,
    event_sink=record_public_activity,
)
```

`NativeLoopToolHost.specs()` 返回 `NativeLoopToolSpec(name, description, input_schema)`。
`invoke(name, args)` 返回：

```json
{
  "success": true,
  "content_items": [
    {"type": "inputText", "text": "工具结果 JSON 或来源文本"},
    {"type": "inputImage", "imageUrl": "data:image/png;base64,..."}
  ]
}
```

工具名、输入 schema、来源绑定、模型和运行 binary 身份固定到会话。当前业务版本可以在该任务中
推进；原文件身份不能改变。无法履行原工具合同的变更需要建立新任务；仍履行原工具合同的 Skill 能力升级，
可以通过下述显式 capability epoch 在同一线程继续。两者都不能静默改变已有任务含义。
来源、反馈及写入权限由工具宿主判断，不能让文档中的文字授予权限。工具错误会返回给 Agent 修正；线程、轮次、
调用身份不匹配会停止处理。

图片工具用实际 App Server 的 `inputImage/imageUrl` 返回来源图片，不把文件路径字符串冒充已看过图片。
当前 JSONL 单帧限制为一 MiB，工具结果超过 950,000 字节会返回明确的缩小读取范围提示；图片宿主需要裁剪或
缩放到合适大小并保留原图定位。导出仍使用原图，不使用阅读预览替代原件。

## 持久化与恢复

每个任务的私有目录保存：

- `run-state.json`：绑定、真实 thread/turn、活进程、最近状态和回执。
- `receipt-<attempt>.json`：每次调用的终态/不确定态、真实工具调用、使用量、先前轮次及快照验证。
- `events.jsonl`：公开的 Agent 说明、工具动作、送达反馈和运行状态。隐藏推理正文不落盘。
- `calls/`：逐工具输入和返回值，用于追踪与同 callId 重放。它们可能含来源内容，必须留在私有运行存储。
- `workspace/`、`sqlite/`、`logs/`：固定技能资源及持久运行状态。身份凭据仍通过已授权的专用 Codex Home 使用，
  不复制到任务目录。
- `tool-specs.json`：线程实际登记的完整工具定义及哈希。恢复始终使用原定义；宿主可以增加工具或放宽参数，
  只要仍能履行旧合同。新增工具不会静默改变该线程的工具集合。

同一任务采用文件锁，第二个活执行者会收到 `NATIVE_LOOP_SESSION_ALREADY_RUNNING`。
收到超时或传输异常时记录 `UNKNOWN`，关闭并确认本次子进程，再保留真实身份供接续。`UNKNOWN` 不是任务完成，
也不是允许重放变更的证明。恢复时若服务端仍有 `inProgress` 轮次，拒绝启动重叠轮次。

工具调用前写入 `INVOKING`，返回后写入 `RETURNED`。同调用重送可返回已保存的结果；若进程恰在变更后、
保存回执前中断，返回 `TOOL_OUTCOME_UNKNOWN_INSPECT_WORKSPACE_BEFORE_RETRY`，不直接重复变更。
共享业务工具还须使用候选哈希、预期版本和原子保存做幂等或状态核对。应用发布是否成功始终以业务状态为准。

早期会话仅保存工具哈希时，先用现有定义核对原哈希再回填；若宿主已增加能力，则只读该任务私有、版本固定的 SDK
`thread_dynamic_tools` 注册表，按实际 thread ID 恢复原定义，并再次要求完整哈希完全一致。此路径不读取消息、隐藏推理、
凭据或其它线程，不开新模型轮次。无法精确恢复或宿主已缩小旧能力时明确报告兼容问题，不通过重建线程掩盖。
参数兼容检查采取保守方式，仅接受已证明的扩展（例如枚举增加、可选参数增加、范围放宽）；继续使用原冻结插件。

缺少必要语义能力时采用显式 capability epoch，而不是覆盖旧包。宿主先登记通过构建哈希核验的包，
`POST /delivery/loop/{task_id}/capability` 接受 `command_id`、`bundle_sha256`、`origin`、`reason`；只有实际轮次已核验终态才可暂存升级。
新包复制到 `task/capabilities/<SHA>/reqpilot`，旧包、旧技能工作区、业务工作区和原 thread 保留。随后 `/resume` 使用相同 thread 和原工具合同，
在新的 `runtime/capability-workspaces/<epoch>` 中加载新 Skill，记录 old/new 哈希与授权引用。升级暂存和运行时真正激活分别显示。
新增语义继续通过原 `review-revise` 等既有工具的扩展输入进入，不需给原线程偷偷扩展 operation 枚举。

真实合成验证已证明：同 thread、同工具 SHA、新 Skill SHA，新能力前缀生效，旧反馈与原始随机证据码保持。
证据为私有 `native-loop-development-20260908/capability-upgrade-verification.json`。首次验证发现 SDK 的 `thread.cwd` 可以保持历史目录，
而顶层 `cwd` 是恢复后的有效目录；适配现分别核对二者，不再错误地要求它们始终相等。

E01 的首轮 35 页实跑在上下文压缩事件处暴露 `contextCompaction` 被误拒绝的宿主缺陷。现已把它作为正常生命周期事件处理，
只保留开始/完成和身份，不记录压缩内容。原线程已通过只读 `inspect_session` 核验为 `interrupted`，没有为核验创建新模型轮次；
原错误回执与后续独立 observation 同时保留，不把原错误改写成成功。证据为私有 `agent-loop-20260908/raw-e01/terminal-reconciliation.json`。

E01 随后已实际通过 HTTP 登记并激活 r8 能力包，保留原 `301e9a90…` 包与全部工件，任务仍为
`loop-0a2525e363d15efe36a34760b6dda3c7`、线程仍为 `01a07e87-b5cc-79b2-ba5b-ef26dfacf38d`。
新 job 为 `job-c86988bd0b644ad9aa0fefbdbbf522ec`，新 turn 为 `01a07ed4-50ed-78d3-be39-21eab399b173`，
能力 epoch 为 `cap-a35069143db6980eb842f071`，包 SHA 为 `aa161df2460d20889d46d436e53fdad7efb60dc8ee525a29ee81333afea3ee7b`。
此轮完成了分类/编号、18 个派生子项、341 个单元分析及预备 Excel；由于真实 Map 标题/正文与 scope 原子修订能力缺口，
Agent 保留候选而未请求发布，完整 35 页业务目标尚未验收。升级请求、响应、恢复目标和新轮次身份保存在私有
`agent-loop-20260908/raw-e01/epoch-*.json`。

该轮又暴露 SDK 历史投影错误：恢复后的首条事件重复旧 ordinal，SDK 持续报告 `expected ordinal 2150, got 2149`。
虽然实际 `turn/completed` 已送达，`thread/read` 与 `thread/turns/list` 均看不到新轮次。原 durable job 因严格终态核验为 `FAILED`，
没有将它改写成成功；全部预备业务工件保留。修复在隔离副本调用同一 SDK 的历史迁移，再通过
`native_loop_history.py` 验证全部 3,344 条记录顺序与 payload 一致，仅接受 SDK 的 ordinal 重排和可选 `content:null` 规范化。
原 rollout 字节不改，旧 SQLite 与 runtime state 保留为可回滚备份；只为当前 thread 导入 SDK 实际生成的派生历史行，
并把该 thread 的 rollout 指针移到任务内重建副本，不手写 turn 状态或新建线程。

恢复后冷 `inspect_session` 实际读到原 turn `interrupted`、r8 turn `completed`，原工具合同 SHA 保持，未调用 `turn/start`。
回执为私有 `raw-e01/r8-history-adoption.json` 与 `r8-repaired-cold-terminal-verification.json`；原错误、重建证明和后续 observation
分别保留。此恢复是针对已定位 SDK 索引故障的显式本地修复，不是每轮清空历史的常规运行步骤。通用接口能力参考
[OpenAI App Server 文档](https://learn.chatgpt.com/docs/app-server)，具体兼容行为仍以本次固定二进制的实际协议和回执为准。

r11 首个独立反馈轮次又遇到 `thread.status=systemError`。旧 native ledger 继承了 canary 的立即失败策略，在详细错误或终态
到达前关闭了进程。已通过同线程只读快照确认该轮 `interrupted`，95 次工具记录和已写候选全部保留，无法据已有记录推定底层服务错误的具体原因。
原生宿主现将这种状态记录为通知，最多等待 30 秒收取具体错误与终态；`willRetry=true` 交由 SDK 自行重试，超时仍记不确定态，
不补造完成。错误只存必要代码、重试标志及相关摘要哈希；不存错误正文。通用 canary/stage 的严格默认保持。
动态工具 schema 错误补充违规字段/规则（例如额外 `Push` 字段），不回显客户输入正文。协议序列、宽限超时和严格身份测试通过，
回执为私有 `raw-e01/r11-native-error-drain-fix.json` 与 `r11-system-error-terminal-observation.json`。

随后同线程 r11 接续在配置的 3,600 秒上限结束，保留 681 次工具与最后的图注候选、交付和 close 输入，未代为发布。
长历史 `thread/read(includeTurns=true)` 达到既有 32 MiB JSONL 上限。锁定的本地 SDK 实际提供
`thread/turns/list(itemsView=notLoaded)`，原生 inspect、resume 前后和最终状态核验现使用该分页接口，
`thread/read` 只读元数据，`thread/resume` 设置 `excludeTurns=true`。每页至多 100 turns、至多 100 页；
重复游标、重复 turn、异常状态、非空 item 或超限会拒绝核验，保留 UNKNOWN，不扩大 canary/stage 上限。
真实同 turn `01a07fa2-8322-7131-a5d5-de5750244235` 已核 `interrupted`，保留全部 5 个 turn，最大状态响应仅 4,640 字节。
回执为私有 `raw-e01/r11-second-turn-timeout-observation.json`，没有新建线程或调用模型。


本入口将宿主工作区的 `project_doc_max_bytes` 设为 0，并验证 `instructionSources`，避免任务目录位于仓库
内部时自动吸入开发者 AGENTS；Agent 使用显式 Skill。动态工具可用，环境中的 shell、网络、其它插件及其它
项目文件没有自动授权。

## 已执行验证

聚焦测试：

```bash
PYTHONPATH=prototype prototype/.venv/bin/python -m unittest prototype.tests.test_native_loop_driver -q
```

2026-09-08 的真实只读协议验证使用独立合成来源和图片，未改 PDS、未提交业务候选。证据位于私有
`prototype/runtime-data/native-loop-development-20260908/`：

- 首轮 Agent 自行调用两次动态工具读取文字和图片；活轮次 `turn/steer` 收到新增格式要求，最终答案包含
  工具返回的随机证据码及新增前缀，终态快照为 `completed`。
- 第二轮在工具完成后被取消，终态快照为 `interrupted`。
- 第三轮恢复同一 thread，重新读取来源，最终仍保留第一轮反馈要求，终态快照为 `completed`。

以上证明工具传输、在线反馈和持久线程的中断恢复，不能证明需求分类、图片释读或全业务交付已经通过。
业务验收必须继续用同一插件工具完成真实来源修订、自查、发布和 Excel，并核查原文、人工决定及版本连续性。

## 主应用任务接口

生产适配在 `source_item_loop_jobs.py`，durable job kind 为 `agent.source-item.loop`，沿用
`source-item-workbench-v1` marker。专用工作台 worker 的 `serve` 会注册这一类型；不得为它启动领取全部历史任务的 worker。
任务首次准备时调用同一插件的 `build_bundle.py` 冻结完整技能、脚本和领域运行代码，此后每个 CLI 操作直接使用任务包，
不依赖开发仓库。原文件、当前 Map/基线、已有派生/分析/图注及知识原件登记导入 portable review workspace。

接口前缀为：

`/api/projects/{project_id}/sor-documents/{document_id}/agent-structure/delivery/loop`

| 方法与路径 | 输入或用途 |
|---|---|
| `POST /loop` | `command_id`、`expected_source_sha256`、`expected_baseline_sha256`、`goal`；可传 `origin`、`source_item_ids`、已有 `feedback_ids` |
| `GET /loop/latest` | 从应用索引找文档最近任务；浏览器状态不是唯一任务索引 |
| `GET /loop/{task_id}` | durable 与 runtime 状态、真实 thread/turn、当前 review SHA、业务结果和允许的控制动作 |
| `GET /loop/{task_id}/events?after=0&limit=100` | 按字节游标增量读取公开事件；返回 `next_cursor` |
| `POST /loop/{task_id}/steer` | `command_id`、`feedback`；可传 `origin` 与 `source_item_ids`。先保存版本绑定反馈，再送入活轮次 |
| `POST /loop/{task_id}/interrupt` | `command_id`；请求中断，并保留已形成的工作内容 |
| `POST /loop/{task_id}/resume` | `command_id`、可选 `goal`/`origin`/`source_item_ids`；以新 durable job 继续同一任务与线程 |
| `POST /loop/{task_id}/finalize` | 仅 `command_id`；原轮次实际已完成且同版 publish/close/deliver 回执齐全时，用新的宿主 job 重试保存，不启动模型 |

`origin` 为 `USER_REQUEST` 或 `AGENT_SELF_REVIEW`，默认来自界面的前者。Agent 自查不能通过工具冒充用户反馈。
`task_id` 是稳定的不透明身份，`job_id` 是当前轮次身份。重复命令不会因为工作版推进而再启动一次同样的模型任务。
初始接口针对已有同源基线的修订闭环；从新原文件开始的提取调用仍由 portable 工具提供，不能把此接口范围扩大声称为
应用内全部新文档入口已经验证。

返回的 `job_status=SUCCEEDED` 只表示这一轮 durable handler 正常终结。`business_status` 和 `result.status` 区分
`NEEDS_CONTINUATION`、`INTERRUPTED`、`REVIEW_COMPLETE`、已发布原文后继等实际成果；`application_saved` 表示结果是否经
应用发布，人工接受始终另行记录。已成功发布后再 `resume`，以实际最新应用基线作前版绑定，只提交其后的私有版本链。

公开事件类型限定为 thread/turn 开始与终态、Agent 公开说明、工具开始/完成、反馈送达/失败和运行错误。
接口只返回这些类型对应的固定字段，不传工具的大块来源输入、原始 reasoning、reasoning delta 或计划 delta。
完整工具结果仍位于授权的私有任务目录，可供宿主核对。

## 保存与并发

`reqpilot_publish` 只记录请求。模型轮次真实 `COMPLETED` 后，宿主再次调用同包 `review-verify` 检查整个不可变 review 历史、
来源/SRC、派生/分析、知识原件、矩阵快照和 Excel 字节，再准备应用工件。多次 `review-close` 形成的私有后继按顺序保存为
连续 product artifacts，最终任务成功才切换当前版本。只改变分析、派生或图注时保存独立审阅工件，原文基线 SHA 不变。

来源工作版的最终并发栅栏位于 `PostgresJobQueue.succeed`，与任务变为 `SUCCEEDED` 处于同一事务。它取得与 Document Map
更新共用的文档来源锁，验证实际原文件、Map head 和当前 SourceItem 前版。并发冲突令该任务为 `FAILED`，保留未发布工件，
不会把新结果切成当前版本。原子检查不能由 handler 内较早的一次读取或“开始保存”状态代替。

迁移 0036 可用时，宿主从共享 analysis-unit 编译器构造发布前后人工意见上下文，不接受 Agent 的人工决定包。
原生图谱、普通审阅、直接/分阶段基线修订、scope 修订/撤销、独立派生和图片路径在同一发布事务调用人工仓库 hook，
读取模型运行期间的最新用户版本。语义变化或单位消失即持久保存 STALE；后续恢复原文也不自动激活旧意见。
同 baseline 的派生/图片变更另检查最新语义发布 job 身份，不能仅凭 baseline SHA 相同就覆盖并发的新上下文。
完整上下文仅为宿主内存参数，job 结果保存简明 rebind 回执；缺少 0036 时保持原行为、不偷偷创建生产表。
旧 D2 单独 Map 发布没有已验证后继 SRC，上述源锁事务将旧人工意见保守失效为 `MISSING_PREVIOUS_CONTEXT`；
保留最后已知基线与意见历史，不把空上下文声称为新 SRC 提取完成。完整原生 Map+SRC 图谱仍按真实 before/after 精确继承。

此次 92 项入口聚焦回归通过，新增独立 PostgreSQL 验证覆盖分类、同基线派生、同基线图注的各两类 job 竞争，均一胜一冲突，
最新用户文字保留且 STALE 与发布同事务提交；在 hook 后注入失败验证两者全回滚。旧 D2 Map 同样实测失效、回滚及幂等重放。
回执 `raw-e01/manual-publication-hooks-postgres.json` 记录 0036 在本次 UUID 临时库执行和该库删除，生产是否已部署另由服务回执说明。


原文版本的 provenance 使用 `SourceItemNativeLoopRunReceiptV1` / `CODEX_APP_SERVER_NATIVE_LOOP`；真实持久 thread 不伪装成
清理成功的 JSON stage，也不标记为 `CODEX_APP_DIRECT`。回执要求真实终态快照和成功的发布请求工具记录。后续工作台、分析、
拆分和导出从同一 loop 工件取得派生/分析/图注；知识或分析单元改变后旧建议保持历史并明确失效。

目前聚焦的 66 项测试通过，覆盖 driver、loop、direct revision、baseline jobs、analysis/decomposition jobs、images 和 review。
另外已实际制造一次观察超时，确认该进程终结后读取同一 thread，再恢复成功；服务端把先前轮次核对为 `interrupted`。
这些证据不替代真实业务的应用级验收。

独立 PostgreSQL 并发实证已完成：新建本次专属临时数据库、应用 35 个迁移，两个真实线程同时提交同一项目、文档、
来源、Map 与前版 N 的候选。一个任务为 `SUCCEEDED`，另一个明确为 `FAILED/source_item_publication_conflict`，其结果没有
进入当前版本；旧 N 和两份候选对象字节保持不变。随后仅删除本次创建的数据库，并查询确认已经不存在，生产数据库未迁移。
回执为私有 `native-loop-development-20260908/postgres-publication-verification.json`。可复现命令：

```bash
REQPILOT_RUN_NATIVE_LOOP_POSTGRES_E2E=1 \
PYTHONPATH=prototype prototype/.venv/bin/python -m unittest \
  prototype.tests.test_native_loop_publication_postgres -q
```

该测试默认跳过，只允许本地 PostgreSQL，创建 UUID 命名的独立数据库；不会对传入连接所指向的原数据库执行迁移。
可选 `REQPILOT_NATIVE_LOOP_POSTGRES_RECEIPT` 指定私有回执路径。验证范围为真实事务竞争和原子可见性，未把合成工件当成
需求语义或业务质量验收。


## r12 实际历史修复与只重试保存

r12 原模型轮次已发出真实 `turn/completed`，180 次工具中包含成功的同版 deliver、review-close、review-verify、reqpilot_publish。
SDK 在续接首条 `thread_settings_applied` 重复了旧末 ordinal 6210，导致派生索引停滞；宿主严格终态快照校验将原模型 job
`job-7c54e5a59ce247be86dfc7a895012fcf` 留为失败，没有据 Agent 最终文字直接保存。该失败与模型实际完成是两个不同事实。

现有本地 SDK 原生维护命令为 `codex migrate-rollouts --apply --thread <thread_id> --json`。宿主封装仅对停写任务的隔离副本运行，
复制目标 thread 的 SDK 元数据，把副本 `history_mode` 暂设为 legacy 触发 SDK 自己的迁移，随后要求全部记录顺序和 payload 相等；
差异白名单只有 ordinal 连续重排、缺省 reasoning `content:null` 的 SDK 规范化。不会改正在写的 rollout、补造工具或终态。
通过后备份原 SQLite/runtime state，仅切换目标 thread 的实际 SDK 派生索引和新 rollout 指针，再冷分页核全部 turn 与原工具合同。
回执写入位于 SQLite 更新后、事务提交前；注入写失败已证明索引/指针一起回滚。已留失败的确定性目录必须先检查，不能盲目重试同输入。

可重复的受限入口（省略 `--apply` 仅检查）：

```bash
PYTHONPATH=prototype prototype/.venv/bin/python -m app.adapters.agents.codex_app_server.native_loop_history \
  --session-dir /absolute/task/runtime --codex-bin /absolute/codex \
  --codex-home /absolute/private-codex-home --repository-root /absolute/ReqPilotX --apply
```

真实 r12 重建覆盖 7,009 条记录，仅 798 个 ordinal 与 499 个可选 null 规范化；原 rollout SHA `4697dca…` 保留，
新副本 `4ab6ed…` 冷 SDK 读取确认原 r12 turn `01a08007-6c9c-7401-9192-225f42d6c8ba` 为 completed，全部 6 个 turn 保留。
回执是私有 `raw-e01/r12-history-reconciliation.json`。失败保留 UNKNOWN 和可回滚备份，不授权发布。

FINALIZE 重新核实际 completed 事件、冷 SDK 终态、原 attempt 文件 SHA、该轮成功 publish/close/deliver 调用和全部封存文件/Excel SHA。
新的宿主 job 保留原输入、能力 epoch、来源/Map/基线/图谱前驱，复用共同发布器及 lease/PG CAS；原失败 job 不回写成功。
真实 job `job-74d3bd1565ae41f1a40e4ae5085e9efa` 已 SUCCEEDED，并被现有 latest/source graph 当前版本读面识别。
同 task/thread/原完成 turn 保持，不新增模型调用、不重复反馈、不由宿主编写候选。
未来原生运行仅在已收到真实 completed 事件且索引终态检查失败时自动调用上述窄恢复流程；其它失败继续保持原严格行为。
这条自动接线已通过聚焦测试；当前 r12 的实际恢复走的是同一显式入口。恢复及 FINALIZE 共 42 项聚焦测试通过。


后续 r13 同线程局部修订已正常完成并保存，实际 state/export HTTP 200；本轮 ordinal 连续，自动恢复分支无需触发。
自动窄恢复已随服务加载，其触发守卫有聚焦测试；真实索引故障修复实证仍是上述 r12 同 SDK CLI + FINALIZE，不能把健康轮次算作故障注入验收。
当前交付是可试用 PDF 功能版本，精确业务范围、正式对象及下载回执见 [新原文件自主提取](AppServer新原文件自主提取.md)。
