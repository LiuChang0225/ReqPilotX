# Extraction Active Context

更新时间：2026-04-03 13:40 CST  
分支：`main`

## 分支目的

- 在 `main + 8000` 上做上线前总体验收
- 收口 code review、功能测试和发布风险，不把高风险问题带到服务器
- 保持 live workflow / review / matrix 现有逻辑不变

## 当前样本

- live 验证页：`/`、`/workspace`、`/workspace?stage=review`、`/project-progress`、`/phase0-review`
- smoke 项目 / 文档：`codex-aliyun-mini-101619` / `aliyun_sor_XXXX.txt`
- Phase0 目录：`eval/iterations/r27`

## 最近已修

- 本轮未改代码；已完成上线前静态审查和验证
- `unittest discover` 通过 `330/330`
- `table_asset_regression_gate` 通过 `137/137`
- `ai_smoke_suite.py` 补 `PYTHONPATH=.` 后通过 `6/6`
- 真实浏览器已复查首页、工作台、进度看板、Phase0

## 当前已知问题

- `app/db.py` 的 SQLite 连接初始化失败时会泄漏连接，测试里已出现 `ResourceWarning`
- readiness 当前按“任一 profile 可运行”判 ready，不够适合作为发布门禁
- 上传接口会在请求内同步做 `read + parse + knowledge sync`，单实例发布有阻塞风险
- `scripts/ai_smoke_suite.py` 缺少路径引导，裸跑不可用
- 首页仍依赖 Tailwind CDN，`workspace` 仍有既有的 multiple forms DOM 提示

## 当前验证结论

- `/`、`/workspace`、`/workspace?stage=review`、`/project-progress`、`/phase0-review` 均能正常加载
- 当前主要风险不在 UI 壳层，而在 SQLite/health semantics/长请求阻塞/QA 脚本可复跑性
- 当前 smoke 项目里 `Aliyun` knowledge sync 记录仍为 `ERROR`，默认 `Azure` 路径仍可运行

## 下一步

- 先修 SQLite 连接泄漏与上传长请求问题
- 再收紧 readiness 门禁语义并修好 `ai_smoke_suite.py`
- 修完后重跑 smoke / table gate / 浏览器检查，必要时补一次真实导出与 AI 主流程验收
