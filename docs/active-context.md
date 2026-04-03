# Active Context

更新时间：2026-04-03 13:40 CST  
适用仓库：`/Users/liuchang/Documents/New project`

长期背景：[`/Users/liuchang/Documents/New project/docs/项目.md`](/Users/liuchang/Documents/New%20project/docs/项目.md)

## 当前目标

- 上线前做一轮整体 code review、功能测试、发布风险梳理
- 确认 `ReqPilotX` 主流程在 `main + 8000` 下可发布，不把已知高风险带到服务器
- 保持 live AI / review / matrix 流程现状不回退

## 活跃分支 / 工作树

- 当前权威分支：`main`
- 当前远端对齐：`HEAD == origin/main == c537a85`
- 并行 worktrees：`codex/extraction-optimization`、`codex/screenshot-precision-maintenance`、`codex/table-asset-lifecycle`、`codex/table-assets-mainline`

## 当前样本

- 当前 live 验证页：`/`、`/workspace`、`/workspace?stage=review`、`/project-progress`、`/phase0-review`
- 当前 smoke 项目 / 文档：`codex-aliyun-mini-101619` / `aliyun_sor_XXXX.txt`
- 当前标注样本目录：`eval/iterations/r27`

## 最近已修 / 已验证

- 本轮未改代码；已完成上线前静态审查 + 自动化验证 + 真实浏览器检查
- 自动化结果：`unittest discover` 通过 `330/330`，`table_asset_regression_gate` 通过 `137/137`
- AI smoke 在补 `PYTHONPATH=.` 后通过 `6/6`
- 真实浏览器可正常打开 `/`、`/workspace`、`/workspace?stage=review`、`/project-progress`、`/phase0-review`

## 当前验证结论

- 当前主页面与核心只读接口在 `8000` 上可访问，浏览器检查未出现 500
- 自动化主干回归总体为绿，但发现若干上线前应处理的工程风险
- 当前 smoke 项目里 `Aliyun` 知识同步记录仍是 `ERROR`，`Azure` 默认路径仍可运行

## 当前 blockers

- `prototype/app/db.py` 的 `get_conn()` 在 `yield` 前做 SQLite PRAGMA 初始化；若这里抛错会泄漏连接，测试已出现 `ResourceWarning: unclosed database`
- `prototype/app/runtime_readiness.py` 当前只要“任一 profile 可运行”就返回 ready；这会让默认/主发布路径异常时仍可能通过健康检查
- `prototype/app/main.py` 的上传接口会在请求内同步 `file.read()`、`parse_document()`，知识文档上传还会同步做检索写入；单实例/单 worker 发布下容易卡请求或超时
- `prototype/scripts/ai_smoke_suite.py` 直接运行会因缺少 `sys.path/PYTHONPATH` 引导而失败，当前 QA 脚本存在可复跑性缺口
- 首页仍使用 Tailwind CDN，`workspace` 仍有既有的 multiple forms DOM 提示

## 下一步

- 先修 SQLite 连接泄漏 / checkpoint 策略，再补一轮高并发或长任务稳定性验证
- 收紧 readiness 语义：至少区分默认 profile、发布主路径、备用路径
- 把上传链路改成可流式落盘或后台任务，不要在请求内做完整解析和知识同步
- 修复 `ai_smoke_suite.py` 的可执行入口，再重跑上线前 smoke / export / AI 主流程检查

## 活跃端口

- `8000`：本地 ReqPilotX live 服务；本轮 UI 验证使用此端口
- `6333`：本机原生 Qdrant HTTP
