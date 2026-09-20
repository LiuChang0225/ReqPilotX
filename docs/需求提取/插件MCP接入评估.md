# 插件MCP接入评估

2026-09-13后续：用户指定的侧边栏ChatGPT 6 Pro评审已完成，已按“业务合同先行＋固定CLI＋可选stdio MCP”实施，见[本轮实施与评测](插件MCP实施与评测20260913.md)。下文保留2026-09-12的评估事实与当时状态。

2026-09-12。用户询问是否应通过MCP让Agent更顺畅地使用ReqPilotX工具，以及此前没有这样封装的考量。本轮核对代码、安装源和历史记录，形成架构建议；未创建server、安装依赖、修改连接或启动模型测试。

## 当前判断

建议为独立插件增加一层薄的MCP工具适配器，复用已有任务工作区和业务核心，保留CLI供调试、自动化及不支持MCP的宿主使用。它不是业务内核、第二个任务队列或Agent编排服务。仅将自由参数的`run(operation, arguments)`搬进MCP，不能解决现有发现/参数问题。

## 历史事实及判断边界

- 项目以前做过MCP。`prototype/app/adapters/agents/codex_app_server/read_only_mcp.py`保留本地stdio只读实现；`reading_mcp.py`、`patch_mcp.py`、`semantic_mcp.py`等适配器和测试仍在。它们服务于旧阶段/特定run，不等于当前插件的通用工具门面。
- [历史B2记录](../archive/2026-09-03/architecture/codex-app-server-branch-plan.md)记载2026-08-21的6个只读工具发现及真实Sol调用验证。旧代码使用当时的协议与描述符约束，不能原样作为今天的插件server发布。
- 当前插件manifest声明skills，没有随包启用的MCP配置；README明确使用可独立运行的Python CLI，其他coding agent不需理解Codex manifest。当前`workspace init/status/recover/run`已承担来源/依据填写、输出分配、冻结输入、提交与同步，不应在MCP层重复实现。
- 原生应用的`NativeLoopToolHost.specs()/invoke()`经App Server dynamicTools注册，已有任务绑定与回执机制；应用内并非没有结构化工具入口。

在本轮查到的记录中，没有找到一份明确否定“独立插件MCP”的完整取舍记录。能确认的是后来的实现选择了“共享核心＋可移植CLI”，应用内另用注册工具。减少额外进程/依赖/协议维护、先验证业务能力，是这条路线合理的工程解释，但不应伪称为当时已经逐项确认的决策。可移植性也不排斥MCP。

## 预期收益与不能替代的工作

| 环节 | MCP适配可承担的增量 | 仍由谁负责 |
|---|---|---|
| 发现工具 | 宿主发现工具及逐项参数schema，减少查脚本/命令行帮助 | skill说明业务目标、方法及何时选择工具 |
| 调用与输入 | 直接接收结构化决定，由程序冻结输入并调用现有workspace操作 | 既有编译器及版本校验；不新增另一套语义规则 |
| 上下文与接续 | 显式任务句柄、当前状态、结果版本和下一动作 | task workspace/review/index及应用保存的真实状态 |
| 原文与结果 | 返回紧凑结构化结果、原图或可读取引用 | 现有读取/渲染、来源校验及完整工件 |
| 工作台 | 返回当前项目/文档/任务的真实路由和阶段状态 | 既有前端呈现；Agent按skill实际打开，MCP不自动生成界面 |

MCP不自动改善表格/章节理解，不会自动补齐业务背景、修正结束判据或消除重复验证的程序开销。当前CLI也已具备一部分任务路径管理能力，不能把这些既有收益算作未来MCP的新效果。

## 建议形态

`Agent + skill → MCP薄适配 → 当前workspace/application能力 → 共用编译器、工件、工作台`。

CLI与App Server适配器使用同一层能力。优先选择少量有明确参数的任务、读取、结构修订、条目修订、反馈、状态/交付工具；名称与字段在实施时从现有公共命令/业务模型导出，不在此建立第二套合同。普通调用不让Agent填写已知来源/Map/baseline/输出路径；多任务调用显式携带任务身份，不能依赖server内“最近一次当前任务”。

可先验证本机stdio，由宿主启动本地子进程；无需为本地文件工具先建立云服务。server重启后从实体工作区恢复，文件/应用状态不放在MCP进程内。当前任务固定runtime、幂等、版本冲突、自主执行和明确确认策略继续沿用；不恢复旧阶段的读取白名单或新增逐项人工门禁。旧MCP可复用协议/测试经验，但旧run descriptor和阶段约束需重新审定。

Codex当前官方插件文档区分可移植MCP配置与公开提交方式，本机私有安装、公开目录、云端宿主的分发要求不能混用。实际选用的manifest、协议/SDK版本、自动启动和图片返回方式应按目标宿主验证，不凭协议标准宣称跨宿主全部可用。

## 最小验证建议

先做一个完整任务切片：工具发现→获取当前任务→读取原件→保存可核对结构→自查/局部修订→返回真实工作台成果。通过后覆盖条目、反馈及同版程序导出。用同一核心、输入、skill及Sol high/Luna max比较CLI与MCP；分别记录参数/路径错误、机械调用、首次成果、完整结束和语义质量。不要同时修改业务规则后将全部改善归因于MCP。

当前独立插件`cold_run.py`使用`--disable plugins`、`--ignore-user-config`并显式提供工作区技能，以隔离环境影响；原生lifecycle也有清空环境MCP及检查未授权MCP的路径。MCP试验需显式接入并验证本次ReqPilotX server/tool schema，同时保持其他环境能力隔离。不能沿用禁用所有插件的配置后，把server未加载造成的失败计为Agent能力问题。现有CLI冷启动成绩保持原样。

当前仅记录建议，未改变[统一工作流专项](统一工作流与工作台专项.md)的执行顺序，也未将MCP建设设为现有CLI流程的开工前置。

## 核对入口

- 代码：`plugins/reqpilot/.codex-plugin/plugin.json`、`README.md`、`scripts/workspace_tools.py`、原生`native_loop_tools.py`及旧MCP适配器。
- [任务工作区与接续工具](任务工作区与接续工具.md)、[App Server自主任务入口](AppServer自主任务运行入口.md)。
- [MCP工具协议](https://modelcontextprotocol.io/specification/2026-07-28/server/tools)：工具发现、输入schema、结构化结果与内容。
- [MCP stdio](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/stdio)：由客户端启动server子进程。
- [Codex插件构建](https://developers.openai.com/codex/plugins/build)：插件MCP配置及分发边界。
