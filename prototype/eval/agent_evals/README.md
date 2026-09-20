# ReqPilotX Agent Eval 最小套件

本入口统一案例、冻结运行、公开轨迹、来源评核和报告。生产提取仍使用原有 plugin/共用核心，当前Codex执行复用冻结插件runner，Pair使用原生CLI及同一业务MCP；Antigravity仅保留历史观察，不安排新运行；不引入另一套原文模型，不连接即将退出的 OpenAI Evals 平台，也不要求先安装 Promptfoo。

`cases.json` 发布合成五页原件的 Map/full 两个开发 scope、合成阶段微测，以及一项未绑定来源的 PENDING_INPUT 真实留出占位。已绑定客户原件的全文与视觉案例仅留在私有评测目录，使用显式私有 catalogue 输入。阶段预制工件不算模型提取成果，历史案例不重命名为留出。数据边界见[评测契约](../extraction_cases/README.md)。

`rubrics.json` 有 8 个维度、27 项判据：5 项程序检查自动执行，22 项来源语义/过程检查通过绑定版本的 review 记录输入。未提供记录保持 `REVIEW`。不能用总条目数、固定工具顺序、调用次数越少越好或编译器自洽代替来源语义正确。

## 一个入口

从仓库根使用已具备 jsonschema 的 Python：

```bash
prototype/.venv/bin/python prototype/scripts/agent_eval.py validate
prototype/.venv/bin/python prototype/scripts/agent_eval.py --help
```

| 命令 | 作用 |
|---|---|
| `prepare-micro-inputs` | 在私有目录确定性生成两个L2原件、准备recipe、独立判据及bindings；不会自动启动模型 |
| `grade-stage` | 针对STAGE_FIXTURE核对冻结判据、原文定位和实际关联，单列L2成绩，不声称整链通过 |
| `prepare` | 校验私有原件绑定/冻结包，复用 bootstrap 与实际 Codex prompt/SDK discovery，不运行模型 |
| `run` | 执行一次已冻结的原件或明确阶段任务，保存真实公开事件与终态，无整次时长自动截止 |
| `import` | 只读接入已有运行结果、事件和工作区，另存带哈希的历史引用，不改旧成绩 |
| `review-template` | 生成绑定该 run 和工件版本的离线评核表，不给被测 Agent |
| `grade` | 校验引用与工件，执行程序判据，合并明确提供的来源/过程评核 |
| `report` | 形成并列观察表，不把单次、不同变量或截断结果混成模型排名 |

## 私有输入与新运行

原件路径只放私有 bindings，不放公共案例 JSON。例如：

```json
{"sources":{"synthetic-five-page-v1":{"path":"<authorized-original.pdf>","sha256":"<case-declared-sha256>"}}}
```

```bash
prototype/.venv/bin/python prototype/scripts/agent_eval.py prepare \
  --case synthetic-five-page-full --bindings '<private-bindings.json>' \
  --bundle '<frozen-built-plugin>' --out '<new-private-run-directory>' \
  --python '<existing-plugin-and-MCP-venv-python>' \
  --model gpt-5.6-luna --transport MCP \
  --workbench-url 'http://127.0.0.1:18080/workspace?projectId=<isolated-project>&documentId=<isolated-document>'

prototype/.venv/bin/python prototype/scripts/agent_eval.py run --run-dir '<prepared-directory>'
```

当前新运行仅允许 `gpt-5.6-luna/max`、`qwen3.8-max-0902/high`。Pair使用`--host-binary <node> --pair-repo <实际Pair仓库>`，通过MCP运行；默认选择及本地实际发现必须匹配high。历史Antigravity/Sol/Medium结果仍可导入和评分，Antigravity不再进入后续观测。CLI/MCP 两入口共用业务 goal。准备时可省略工作台 URL 仅做 discovery；真正运行前必须另行准备带准确隔离文档 URL 的 run。`run` 是实际模型执行，不能把 prepare 的零模型调用当成已经跑通 Agent。

被测工作区只有原件、冻结包、项目指引和任务目标；rubric、案例标签和评核留在外部 operator 目录。记录的是提示/技能发现隔离，不冒充操作系统级禁止读取其它目录。原件、包、任务指引或 runner 源码在准备后变化会拒绝执行；重新准备到新目录，不改旧 run。

MCP 调用权限只针对本次已授权的工具列表中实际提供的工具，包含可能准备阅读工件的 `source_read`；不改全局配置。工作台原件 SHA 在运行前独立 GET 核对。不跳过宿主规则，不复用旧 pluginSessionId/resultRevision 伪装新冷启动。

`--observation-seconds` 默认 1200，仅保存观察记录，**不杀进程**。观察点的工件索引哈希注明非原子观察，不能冒充完整冻结快照；需要跨时间比较时另行冻结状态。单工具的技术超时继续由原包处理，用户中止与自然终态分别记。`events.jsonl` 仅持久化公开事件，不保存 reasoning 事件。

## 历史接入和评分

```bash
prototype/.venv/bin/python prototype/scripts/agent_eval.py import \
  --case synthetic-five-page-map --result '<old-result.json>' \
  --events '<old-events.jsonl>' --workspace '<old-workspace>' \
  --metadata '<old-operator-case.json>' --out '<new-import-directory>'

prototype/.venv/bin/python prototype/scripts/agent_eval.py grade \
  --run-dir '<run-directory>' --out '<new-grade.json>'
prototype/.venv/bin/python prototype/scripts/agent_eval.py review-template \
  --run-dir '<run-directory>' --out '<private-review-template.json>'
prototype/.venv/bin/python prototype/scripts/agent_eval.py grade \
  --run-dir '<run-directory>' --reviews '<completed-review.json>' --out '<new-reviewed-grade.json>'
prototype/.venv/bin/python prototype/scripts/agent_eval.py report \
  --input '<grade-a.json>' '<grade-b.json>' --out '<comparison.md>'
```

历史导入要求已记录原件/固定包身份、input manifest 及公开事件；缺证据的旧脚本结果不能编造这些字段后导入。`--kind DETERMINISTIC_REPLAY` 只用于确为程序重放且证据齐备的运行，永不计为自主 Agent 成功。模型记录导入不会重新调用模型；历史 task 必须对应被评时点，后续合法续接产生的更晚工件会使旧时点证据失效。

评核表需要实际 reviewer 身份、run manifest 与工件 basis SHA；每个明确 PASS/FAIL/N/A 都有文件 SHA 引用及简短依据。来源判断可以由操作者、独立 Agent 或专家执行，但身份必须如实声明，不能将操作者复核写作独立冷验证或人工接受。离线标注不意味着每份生产文档运行时必须等人工反馈。评核不存隐含思维链。

评核者还应取得[工件解释约定](review-contract.md)。它区分文档单元/直属章节、原文保留/上下文关联和程序/语义验证，防止把字段误读或新提出的工程疑问当成既定失败。

报告把 `execution_classification` 与 `verdict` 分开。`CENSORED` 仅说明在观察时点停止，不能判最终能力；`ENVIRONMENT_BLOCKED` 指已识别宿主阻塞。已保存的截止版本仍可有真实业务 FAIL。自动完整性检查通过而语义未判时，整体仍为 REVIEW；这不推翻历史其它 rubric 的有限验收，只表示本套判据尚未完成。

## 验证与后续

```bash
prototype/.venv/bin/python -m unittest discover -s prototype/eval/agent_evals/tests
ruff check prototype/eval/agent_evals prototype/scripts/agent_eval.py
```

持续评测按成本分层：普通改动跑相关程序测试；工具/skill实质变化再跑受影响 Agent scope；发布候选使用冻结完整流程及独立留出。新失败需先定位，再加入有来源依据的回归判据。当前未集成自动模型裁判、批量重复调度或统计显著性估计，22 项非程序判据不能声称已自动评分。

跨宿主安装、零模型发现、权限与统计差异、真实执行待验边界见[跨宿主记录](../../../docs/需求提取/跨宿主插件安装与评测20260914.md)。

下一批skills入口/阶段微测与指令对照见[分层评测规格](../../../docs/需求提取/Skills分层评测规格20260914.md)。这些准备模式目前是待实现项；现有新运行prepare仍以source-only bootstrap为主，不能把已有完整流程或静态技能校验称为已完成的独立skills eval。

阶段任务在cases中显式声明 `preparation=STAGE_FIXTURE`、`fixture_key`、recipe/oracle SHA；私有bindings提供 `stage_fixtures[key]={path,sha256}`。`prepare` 通过冻结包的真实task service编制预制Map/Items及现有候选，并冻结初始文件清单。模型只收到业务任务、自己的阶段工件和任务ID，operator目录中的recipe/判据不放入提示；这不是操作系统文件读取隔离。首次调用仍使用原有 `prepare` / `run`，阶段评分用 `grade-stage --run-dir <run> --oracle <private-oracle.json> --out <new-report.json>`。

两项 v1 阶段准备已标记 RETIRED，禁止用其启动新任务。`prepare-micro-inputs` 生成 v2 案例与显式 source_form 配方，条件引导段为 PARAGRAPH。历史私有运行使用的评测器快照随私有 run 保存，避免评测器变更影响追溯。

`SCOPED_EXTRACTION` 支持原件冷启动，仅提供用户业务范围，不预制原文答案。公开 `synthetic-scoped-extraction` 用合成原件测试该范围的 rubric 选择；真实 catalogue 通过 `REQPILOTX_AGENT_EVAL_CASES=/controlled/cases.json` 显式提供并使用公开 Schema 校验。程序准备成绩与新会话执行成绩分别记录，不能用合成程序测试宣称真实案例或模型执行通过。
