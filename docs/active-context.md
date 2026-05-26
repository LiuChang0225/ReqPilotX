# Active Context

更新时间：2026-05-16 17:22 CST
适用仓库：`/Users/liuchang/Documents/ReqPilotX`

长期背景：[`/Users/liuchang/Documents/ReqPilotX/docs/项目.md`](/Users/liuchang/Documents/ReqPilotX/docs/项目.md)

## 当前目标

- 当前活跃 extraction 工作在 `codex/extraction-quality-loop`。
- `fix28` 已收掉 `J02.311` 用户反馈残留：`7.5.5 表12` snapshot-only 默认图绑定、`7.6.1.1 图6/图7` source snapshot 复核、`附录 A / 表A.1` source snapshot crop 精度。
- 本轮新增 SERES 台账规则：`SERES-R022`（snapshot-only ref 默认图强制回源图）与 `SERES-R023`（附录重复表题前的说明段不作为裁切截止）。
- 结论：没有新增回归；SERES 与 legacy 指标均与 fix27 持平。

## 活跃分支 / 工作树

- 主线：`main` -> `/Users/liuchang/Documents/ReqPilotX`
- extraction 回归入口：`codex/extraction-quality-loop` -> `/Users/liuchang/Documents/ReqPilotX/worktrees/_active/extraction-quality-loop`

## 当前样本 / 保留工件

- `prototype/eval/extraction_diagnose/j02_311_20260516_fix28`
- `prototype/eval/extraction_batches/seres_mda_20260516_fix28`
- `prototype/eval/extraction_batches/legacy_benchmark_core_20260516_fix28`
- `prototype/docs/seres-mda-rule-fix-context.md`

## 最近已修

- `prototype/app/services/extraction_preview.py`
  - `snapshot_only` 既有 ref 现在会用 `source_snapshot_path` 覆盖旧 `slice_snapshot_path`；`7.5.5 表12` 不再默认展示坏 markdown/slice 图。
  - 显式视觉裁切支持传入 title token；同页存在重复同名表题时，重复表题前的说明段不会提前截断 crop；`附录 A / 表A.1` 第一张 source snapshot 已覆盖 `00 positiveResponse` 表体开头。
  - `7.6.1.1 图6/图7` 当前 source snapshot 已复核，未见错切、重复或串正文；无需新增规则。
- `prototype/tests/test_extraction_preview.py`
  - 新增 snapshot-only slice 覆盖与重复表题 cutoff regression tests。
- SERES 长台账已记录 `SERES-R022 / SERES-R023`，并把 J02.311 残留状态更新到 fix28。
- **本地单机打包与 100% 离线运行 (Pywebview + PyInstaller + Native Qdrant)**:
  - 新增 `prototype/app_desktop.py`（桌面 GUI 启动器），后台线程拉起 FastAPI，并拉起本地预编译的 Qdrant 数据库（不依赖 Docker）。前端在 Pywebview 原生窗口渲染，数据持久化自动重定向到用户主目录。
  - 新增 `prototype/build_desktop.py`（跨平台打包脚本），一键打包静态资源、排除无用重型 ML 库（将体积优化至 301MB），并打包 macOS 和 Windows 的本地 Qdrant 预编译二进制程序。
  - **Uvicorn 日志流与时区兼容修复**：实现 `DesktopLogger` 属性动态委派避免 Uvicorn 报错；通过动态猴子补丁 `SafeDatetime` 绕过主代码库中 `progress_tracker` 时区 naive/aware 减法报错，且对原有后端没有任何侵入性。
  - **安全性与可用性升级**：优化 Qdrant 启动检查为真实的 HTTP 接口轮询健康检测（10s 超时），确保彻底启动后再绑定端口；API 密钥读取路径实现 `环境变量优先 > 本地 config.json 次之 > 混淆密文兜底`；清空了 `app_desktop.py` 中硬编码的 `OB_DASHSCOPE_API_KEY` 以确保安全分发；配置 `.gitignore` 过滤 `prototype/bin/` 文件夹防止仓库污染；系统性清理了 `AGENTS.md`、`track_registry.json`、`docs/*`、`prototype/README.md` 以及单元测试代码 (`prototype/tests/`) 中残留的 `New project` 历史路径。

## 验证状态

- Focused tests:
  - `python3 -m py_compile prototype/app/services/extraction_preview.py prototype/tests/test_extraction_preview.py` -> OK
  - `python3 -m unittest prototype.tests.test_extraction_preview -q` -> `57 tests / OK`
  - `python3 -m unittest prototype.tests.test_extraction_diagnose -q` -> `8 tests / OK`
  - `python3 -m unittest prototype.tests.test_extractor_sections -q` -> `69 tests / OK`
- Focused diagnose:
  - `j02_311_20260516_fix28` -> `1` finding: `parser_low_confidence_hotspot` only。
- Batch:
  - `seres_mda_20260516_fix28`：`batch-report PASS`，`regression-suite PASS`，`quality 99 / 102`，`fidelity 72 / 72`；与 `fix27` 无回退。
  - `legacy_benchmark_core_20260516_fix28`：与 `fix27` 完全同量级，`quality 38 / 40`，`fidelity 80 / 96`；`regression-suite` 与 `oil-pump-benchmark` 仍为历史 FAIL。

## 当前 blockers

- `J02.311` 当前没有新的用户可见 OPEN 项；剩余 focused residual 是 `parser_low_confidence_hotspot`。
- `J02.311` parser 风险页仍是 `1 / 11 / 12 / 13 / 70 / 89 / 105 / 108 / 112`，当前不等价于可见回归。
- `legacy_benchmark_core` 与 `oil-pump-benchmark` 仍是历史红线；当前目标是“不回退”，不是顺手改绿。
- `SERES` 整批仍有 `profile_unmatched = 9` 与 `parser_low_confidence_hotspot = 11` 的审计债务。

## 下一步

- 将 `extraction-quality-loop` 作为 J02.311 用户可见残留已收口状态处理，准备分支收尾/合并或 PR。
- 若后续重启 review 服务，可做一次 `表11 / 表14 / 表12 / 图6图7 / 附录A` live spot-check，但不再阻塞 fix28。

## 活跃端口

- 本轮未重启前端或后端服务。
- 如需前端复核，先确认 review 服务状态并重新 refresh preview。
