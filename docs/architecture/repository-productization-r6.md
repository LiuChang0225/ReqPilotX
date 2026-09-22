# R6：R3、R4、R5 集成与发行验证约定

R6 从 main `7d960a4eea0fd5ecc44cd3777f34bccc593237e6` 建立独立集成线，
保留 Git ancestry 合入以下已经单线审查的固定输入：

- R3 `74f5705ad01bb2c9f86f9472850c81b9f10c7aaa`
- R4 `97595885a75a144e56c28f9a2dc4a1609bb13b3e`
- R5 `409759a6c7e9a6d644340855d27922535b7dd253`

R6 不新增业务功能、不改变 schema、权限或数据策略，也不把三条分支各自的历史
验证拼成组合通过。正式结果必须由同一个干净候选重新构建并绑定其 commit/tree。

## 组合后的权威源与消费者

| 范围 | 权威源 | 直接消费者与要求 |
|---|---|---|
| 业务引擎 | `packages/engine/src/reqpilotx` | server、插件和宿主安装；不得反向依赖 server 或旧 `prototype/app` 实现 |
| 服务装配 | `apps/server/src/reqpilotx_server` | API、worker、37 个包内迁移和运行资源；兼容入口只转发同一实现 |
| 共享工作台 | `packages/workbench-ui` | Web 和 Panel 从同一源码构建；Panel 保持 host contract、外部 React 与卸载合同 |
| Web 外壳 | `apps/web` | 生成 `apps/web/dist`，由 server、Pair 和 desktop 显式消费 |
| Pair | `integrations/pair` | 只使用绝对路径、非 editable engine/server 安装及独立 Web 产物，不回退 checkout |
| Desktop | `apps/desktop` | 从当前候选的 Web、engine/server 和声明资源生成成品；旧 `prototype` 入口只转发 |
| Agent 资产 | `agent-assets` | `.agents/skills`、插件 skills 与 PDF capability 是受校验投影；builder 和 eval 安装门禁复用完整清单检查 |
| Agent Eval | `evals/agent_evals` | 程序评测与宿主安装检查；历史兼容 import 只指向同一实现 |

旧冻结 bundle 的 `runtime/prototype/app` 是历史发行格式，不重算或迁移。任务、队列和
恢复继续绑定完整 `bundle_sha256`；engine core identity 不能替代 bundle identity。
来源 hash、SRC/REQ ID、revision、人工决定、epoch 和既有任务私有包均保持。

## 干净候选与验证顺序

使用项目锁文件准备本线独立 Python/Node 环境，先完成静态说明与必要修复并提交，
再固定干净候选。构建及动态结果写入忽略的 `output/r6-final/<candidate>/`，不写回
候选提交。建议从仓库根执行：

```sh
npm ci
npm test
npm run typecheck
npm run build
npm run build:panel
npm run test:css
node integrations/pair/scripts/build.mjs
npm --prefix integrations/pair/tests/react-host ci --ignore-scripts
PAIR_REACT_ROOT="$PWD/integrations/pair/tests/react-host" node --test integrations/pair/tests/*.test.mjs

prototype/.venv/bin/python -B scripts/test_python.py backend
prototype/.venv/bin/python -B scripts/test_python.py plugin
prototype/.venv/bin/python -B agent-assets/scripts/asset_registry.py --repo . check
prototype/.venv/bin/python -B -m unittest discover -s evals/agent_evals/tests -p 'test_*.py'
prototype/.venv/bin/python -B scripts/verify_engine_release.py --repo . --output /absolute/r6-output/release
prototype/.venv/bin/python -B scripts/smoke_workbench.py --postgres-bin /absolute/postgresql/bin --output /absolute/r6-output/pg-smoke
```

发行验证负责 clean-input、wheel/sdist、非 editable 安装、真实 CLI/MCP、模块来源和
完整资产；保留给 Pair 与 desktop 的产物须从同一候选另行构建并记录 hash。安装版
Pair 使用仓库外 adapter、Web 和 venv 运行现有 `installed-smoke.mjs`。macOS 成品按
`apps/desktop/README.md` 构建并运行现有 artifact self-test。PostgreSQL、Pair、desktop
一次只运行一个本机重任务，且只使用本线私有数据根和进程。

## 结果、边界与回滚

每组结果记录 commit/tree、命令、环境、退出码、测试/跳过数、产物 hash 和日志路径。
GitHub PR head 与 Actions 的临时 merge SHA 分开记录；检查尚未完成时状态为
`CI_PENDING`，不能标为整体通过。

默认不验证外部真实 Pair UI、完整 Agent 会话或新增写能力、付费模型、正式签名/公证、
Windows/Linux 桌面、fresh-machine 和生产上线。它们不阻塞本地既定工程范围，也不能
被本轮结果覆盖。

合并前可停止采用 R6 分支。合并后先停止本版本拥有的 API、worker 和 PostgreSQL，
再按合入顺序回退 R6 特有修复及 R5/R4 集成；不删除用户数据、registry 中旧包或任务
私有包，不重算旧 bundle 身份。数据库和旧任务无需因代码回滚重建。
