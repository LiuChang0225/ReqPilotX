# R5 Agent 资产、评测与实验生命周期

R5 基线为 `9a8c08fd655b6155f3fe02bd5f351253abe16120`。本切片只整理技能、
第三方能力、兼容 Agent Kit、评测、实验及其直接消费者；不修改业务算法、schema、
任务编排、数据库、前端、Pair 或桌面外壳。

## 权威源与兼容入口

| 分类 | 资产组 | 权威源 | 保留入口 / 消费者 | R5 处置 |
|---|---|---|---|---|
| CURRENT | 项目提取循环 Skill | `agent-assets/current/project-skills/` | `.agents/skills/` 供项目发现 | 权威源与发现投影逐文件核对，不再手工维护两份正文 |
| CURRENT | 五个便携业务 Skills 与 references | `agent-assets/current/plugin-skills/` | `plugins/reqpilot/skills/` 供源码发现；新 bundle 的 `skills/` | 构建直接复制权威源；源码入口是确定性投影 |
| CURRENT | PDF vendor `26.905.11957` | `agent-assets/vendor/openai-primary-runtime-pdf/releases/26.905.11957/` | `plugins/reqpilot/capabilities/pdf/` 与新 bundle | 保留上游版本、MIT 声明、manifest、provenance 与逐文件 hash |
| COMPATIBILITY | PDF vendor `26.904.11930` | 对应 versioned vendor 目录 | `agent-kit/` 0.15.2 的锁定路径 | 不升级、不改旧 Kit identity |
| COMPATIBILITY | 阶段式 Agent Kit 0.15.2 | `agent-kit/` | 原生结构/条目兼容流程、materializer、cold canary、validator | 保留完整相对结构与 lock；不把 D0–D6 重写成当前工作流 |
| COMPATIBILITY | 显式 canary Skills | `agent-assets/compatibility/runtime-skills/` | PDF/结构/阅读/语义 canary 的显式 allowlist | 从旧 `prototype/runtime_assets` 迁入并让消费者明确失败，不回退旧路径 |
| CURRENT | Agent Eval 套件 | `evals/agent_evals/` | `prototype/scripts/agent_eval.py`；旧 Python import 兼容包 | 保留 case ID、rubric、schema 和合成 fixture 语义；移动不重发 PASS |
| COMPATIBILITY / HISTORICAL / PRIVATE | 其它 eval 定义、旧 runner、进度输入、私有结果 | `prototype/eval/` 或显式私有根 | 既有 CLI/default path、历史读取与授权本机评测 | 未逐项证明可迁移者保留；客户原件、答案、轨迹和结果不进入源码 |
| EXPERIMENT | Codex canary schemas | `experiments/codex-app-server/schemas/` | server canary 与 `prototype/tests/spikes` | 直接消费者改为显式 development resource；旧 spike 模块继续兼容 |
| UNRESOLVED | `target_review` / `type_review` | 仍在 `prototype/` | 私有反馈、历史结果和本机核对入口 | 未证明反馈连续性前不移动、不删 |

完整机器可读分类见 `agent-assets/catalog.json`、`evals/catalog.json` 和
`experiments/catalog.json`。`CURRENT` 表示当前维护/发行入口，不代表已通过所有文档或宿主的
语义验收；`HISTORICAL` 内容也不拥有当前开发顺序。

## 投影与发行合同

`agent-assets/scripts/asset_registry.py` 是资产根、投影和冻结 bundle 的单一解析器：

1. 所有 catalog path 必须是仓库内规范相对路径；源、投影和 bundle 资产不得含 symlink。
2. `mirror` 投影要求文件集合与字节完全一致；`subset` 用于 Agent Kit / vendor 目录中有其它
   锁定内容的情况，仍要求声明源的每个文件同路径、同 SHA。
3. 每个权威源有 canonical tree SHA。变更技能或 vendor 时必须更新源身份，并重建投影；
   vendor 升级创建新版本目录，不能覆盖旧 Agent Kit 版本。
4. `build_bundle.py` 不再从 `plugins/reqpilot/skills` 的投影反向认定维护源；它从
   `agent-assets` 复制普通文件，并把 42 个当前 skill/vendor 文件及 hash 写入
   `build-receipt.json.agent_assets`。
5. 正式 wheel 派生 bundle 要求 Agent asset 源、投影、catalog、resolver 和 builder 与 engine
   wheel 来自同一 Git commit，且提交字节与工作区一致。开发源码构建仍允许未提交的本地迭代，
   但必须先通过源/投影校验。
6. `verify-bundle` 只读取冻结 bundle 和它的回执；因此可以在没有原 checkout 的目录核验
   缺失、篡改、重复身份和 symlink，不从另一份技能目录兜底。
7. 冻结校验同时枚举固定发现根 `skills/` 与 `capabilities/pdf/`，要求实际成员集合与声明
   完全相同；额外技能、额外文件、根/父级 symlink 和非普通文件均拒绝。根目录的合法工具、
   runtime 与宿主安装标记不被误当作额外资产。评测宿主安装门禁复用同一完整资产清单检查；
   校验代码来自受信任的本工具源码，不执行待校验 bundle 中的代码。
8. 正式发行输入先枚举实际 source/projection 文件（包含 Git 忽略项），再与同一 commit
   的 NUL 分隔完整清单逐项对齐，核对普通文件类型、执行模式与原始 blob 字节。`git status`
   干净不代表输入完整；被忽略或 assume-unchanged 隐藏的文件也不能绕过。发现异常仅拒绝，
   不自动删除、强制提交或静默丢弃文件。开发源码构建的显式迭代方式保持。

旧 R2 bundle 和任务继续使用各自冻结的完整 `bundle_sha256`，不会因新 catalog 或新 bundle
重算身份。Engine core identity 仍不能代替插件 bundle identity。

## 消费者迁移

- `pdf_skill_canary` 从 `agent-assets/compatibility/runtime-skills` 复制显式 allowlist，拒绝空
  allowlist、非法 skill 名、缺目录和越出仓库的解析路径。
- 同一 canary 的 JSON schema 改从 `experiments/codex-app-server/schemas` 读取；该路径仍经
  `REQPILOT_DEVELOPMENT_ROOT` 显式配置。安装服务未配置开发资产时保持清楚失败，不偷读某个
  checkout。
- `prototype/scripts/agent_eval.py` 运行 `evals.agent_evals`；历史
  `eval.agent_evals` / `prototype.eval.agent_evals` import 通过薄 package path 指向同一源。
- Agent Eval 的 runtime artifact catalog 检查改读 R3 engine 权威文件，不再解析已经成为薄别名的
  server/legacy module。
- `prototype/spikes/codex_app_server` 的 Python 模块仍是现有兼容命令；schema 和说明不再把该
  目录冒充权威实验资产。

## 验证边界

无需模型或客户文件的聚焦检查覆盖：catalog/projection/Agent Kit lock、投影篡改、缺资源、路径
逃逸、运行 skill 非法名、冻结 bundle 篡改、无 checkout 读取、正式 Git commit/dirty-input
拒绝、canary schema、现有插件构建边界及全部 Agent Eval 程序测试。完整 R1 发行验证只能在
本线形成 clean commit 后运行；它继续负责 wheel/sdist、安装隔离、Excel、真实 MCP 和正常业务
等价，不由目录移动替代。

本切片不调用模型，不打开客户文件，不迁移私有评测目录，也不把旧成绩重新标成通过。

## 保留的未知与退役条件

- `prototype/eval` 中除 Agent Eval 外的默认路径同时服务旧 CLI、schema、私有 sidecar 和历史
  结果；需逐个消费者迁移后才可进一步拆分。
- `target_review` 与 `type_review` 保存 append-only/private feedback 的入口和读取语义；仅改目录名
  会破坏已有链接与回查，当前明确保留。
- `prototype/spikes` 薄命令仍被聚焦测试与历史操作说明引用；迁移 schema 不证明这些命令可删。
- 外部脚本若直接读取旧 `prototype/runtime_assets` 将得到明确的迁移说明而非第二份技能；仓内
  现役消费者均已接线，仓库外未知消费者需在删除兼容说明前提供证据。

## 回滚

R5 只移动源码资产和改变构建/开发定位，不迁移用户数据或修改旧冻结包。回滚时可整体 revert
R5 提交：恢复旧技能维护位置、`prototype/runtime_assets`、Agent Eval 和 canary schema 目录，
以及原 `build_bundle.py` 接线。已生成的新 bundle 保持其新 identity；不要拿它冒充旧 bundle。
数据库、任务 epoch、已接受版本和人工意见均无需重建或回退。
