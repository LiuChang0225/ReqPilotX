# Active Context

更新时间：2026-05-26 16:00 CST
适用仓库：`/Users/liuchang/Documents/ReqPilotX`

长期背景：[`/Users/liuchang/Documents/ReqPilotX/docs/项目.md`](/Users/liuchang/Documents/ReqPilotX/docs/项目.md)

## 当前目标

- 开辟新分支 `codex/ui-optimization` 专项进行 UI/UX 精细化升级。
- 引入符合舍弗勒官方绿色基调（#00893d）的温和高级 HSL 调色板，并实现亮/暗（Light/Dark）双色主题自由切换。
- 在不重组工作台 HTML 骨架的前提下，通过系统化重构 CSS 变量，确保卡片、表单、表格和 AI 分析状态栏能完美适应深浅两套色彩。

## 活跃分支 / 工作树

- 主线：`main` -> `/Users/liuchang/Documents/ReqPilotX`
- UI 专项优化分支：`codex/ui-optimization` -> `/Users/liuchang/Documents/ReqPilotX`

## 当前样本 / 保留工件

- `prototype/eval/extraction_diagnose/j02_311_20260516_fix28`
- `prototype/eval/extraction_batches/seres_mda_20260516_fix28`
- `prototype/eval/extraction_batches/legacy_benchmark_core_20260516_fix28`
- `prototype/docs/seres-mda-rule-fix-context.md`

## 最近已修

- **主题切换与 HSL 色彩体系重构**：
  - `prototype/app/static/index.html`：增加 `<script>` 加载免闪烁主题配置，并在导航栏添加 `#theme-toggle` 按钮和 SVG 太阳/月亮变形图标。
  - `prototype/app/static/reqpilotx-brand.css`：基于舍弗勒绿色 HSL 基底（Hue = 147）重建设计系统，包含 Light/Dark 两套主题，并定义主题切换按钮的呼吸、缩放及变形规则。
  - `prototype/app/static/apple-system.css`：将原通用变量（如 `--apple-bg`, `--apple-surface` 等）改用统一的 `--reqx-` 对应词绑定，增加暗色模式下红色/琥珀色的可读性范围，并将表单输入项背景改为变量。
  - `prototype/app/static/styles.css`：消除 `.hero`, `.summary-card`, `.card` 以及章节总览 `.section-overview-path-card` 里的写死白/浅灰背景，使其完美支持变量；在最末尾追加 `html[data-theme="dark"]` 组件特有选择器，全面修复 AI 运行日志、事件流、按钮 hover 等在暗色下的显示与反差。
  - `prototype/app/static/app.js`：在事件绑定中添加 `#theme-toggle` 的点击事件处理，支持读取和写入 `localStorage` (`reqx-theme`)。
- **P1-P2 视觉品质与人机协同交互优化**：
  - `prototype/app/static/pdf-viewer.js` & `pdf-viewer.css`：将 PDF 证据链跳转重构为平滑滚动，并新增绝对定位的 Canvas 高亮 CSS 闪烁脉冲遮罩层，执行 1.5s 脉冲渐变后从 DOM 中安全清除，保证无卡顿。
  - `prototype/app/static/app.js` & `styles.css`：重构 AI 判定状态 chip 为半透明 HSL 磨砂质感；对 INFO 行赋以 `.row-info` 样式实施 30% 透明度及淡灰底色视觉降噪。在 AI 判定结果旁渲染 Heuristic/Shadow/Primary/Override 等运行模式勋章胶囊。
  - `prototype/app/static/index.html` & `app.js` & `styles.css`：新增一键切换 Compliance Matrix 判定结果过滤按钮栏（全部/ACCEPT/REJECT/CLARIFY），并实现本地过滤刷新逻辑。
  - `prototype/app/static/app.js`：添加全局键盘快捷键监听：`Esc` 键清除 AI 详情选择，`Arrow Up` / `Arrow Down` 在审阅台（Review）或 AI 分析（AI）列表上下导航，自动滚入可视区并渲染 PDF。
  - `prototype/app/static/home.html` & `home.css` & `home.js`：根据用户要求，已将首页设计和内容完全替换为用户在 `ui/homepage` 分支中自行制作的版本，以契合用户的视觉与内容要求。
- **关系型校验与系统稳定性**：
  - `prototype/app/services/extraction_preview.py`
  - `snapshot_only` 既有 ref 现在会用 `source_snapshot_path` 覆盖旧 `slice_snapshot_path`；`7.5.5 表12` 不再默认展示坏 markdown/slice 图。
  - 显式视觉裁切支持传入 title token；同页存在重复同名表题时，重复表题前的说明段不会提前截断 crop；`附录 A / 表A.1` 第一张 source snapshot 已覆盖 `00 positiveResponse` 表体开头。
  - `7.6.1.1 图6/图7` 当前 source snapshot 已复核，未见错切、重复或串正文；无需新增规则。
  - `prototype/tests/test_extraction_preview.py`：新增 snapshot-only slice 覆盖与重复表题 cutoff regression tests。
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
