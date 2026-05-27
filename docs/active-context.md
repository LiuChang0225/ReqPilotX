# Active Context

更新时间：2026-05-27 17:35 CST
适用仓库：`/Users/liuchang/Documents/ReqPilotX`

长期背景：[`/Users/liuchang/Documents/ReqPilotX/docs/项目.md`](/Users/liuchang/Documents/ReqPilotX/docs/项目.md)

## 当前目标

- 完成 `codex/ui-optimization` 分支的 UI 精细化与配置持久化升级。
- 保证已调整的顶栏间距、单行 Flex 状态徽章栏及简化后的 Aliyun API Key 表单能完美兼容亮/暗模式且测试通过。

## 活跃分支 / 工作树

- 主线：`main` -> `/Users/liuchang/Documents/ReqPilotX`
- UI 专项优化分支：`codex/ui-optimization` -> `/Users/liuchang/Documents/ReqPilotX`

## 当前样本 / 保留工件

- `prototype/eval/extraction_diagnose/j02_311_20260516_fix28`
- `prototype/eval/extraction_batches/seres_mda_20260516_fix28`
- `prototype/eval/extraction_batches/legacy_benchmark_core_20260516_fix28`
- `prototype/docs/seres-mda-rule-fix-context.md`

## 最近已修

- **工作台 UI 精细化与配置持久化**：
  - `prototype/app/static/index.html`：移除导航栏冗余的“条目审阅台”链接；将默认 AI 路径首选设为 Aliyun，并将 Aliyun 设置表单中的所有高级参数输入框（模型名、Base URL、Qdrant 地址等）隐藏，仅保留 DashScope API Key 的输入框，避免界面冗杂。
  - `prototype/app/static/styles.css`：将 PDF 对照侧边栏 `.review-pdf` 的定位由 `top: 16px` 调整为 `top: 90px`，使其在收起/展开时完全避开顶栏的主题切换按钮；重构 `.summary-strip` 及 `.summary-card` 的 CSS 样式为单行 Flex 横向流式状态徽章栏，替代原本宽大的网格卡片矩阵；重构了 `html[data-theme="dark"]` 下的全局交互式按钮、阶段标签（.stage-chip）、分类标签（.kind-chip）、对比矩阵过滤芯片（.matrix-filter-chip）及透明指示栏背景，消除刺眼的饱和亮绿/亮白，改为精致质感的渐变翡翠绿和完美融合背景的炭煤灰色，显著提升视感舒适度。
  - `prototype/app/static/reqpilotx-brand.css`：重构了 `html[data-theme="dark"]` 暗色主题色板，引入“深邃炭煤色 & 翡翠绿 (Charcoal & Emerald)”配色方案（类似 GitHub 暗色调），使用极暗炭煤背景色（#0d0f12）搭配精致中灰色边框（#2e343b）与翡翠绿（#10b981）高亮，显著提升暗色模式下的视觉品质、易读性与高级感。
  - `prototype/app/static/app.js`：将 AI 配置相关 fallback 及 selection 默认值均更新为 `aliyun`。
  - `prototype/app/services/ai_copilot.py`：新增 `_load_runtime_config_from_file()` 和 `_save_runtime_config_to_file()`，支持在保存 AI 路径时将所有当前配置项（包含 `REQPILOT_ALIYUN_API_KEY`）写入本地持久化 `config.json`；优化默认 Profile 自动解析排序，默认首选 `aliyun`。
  - `prototype/app_desktop.py`：更新配置加载逻辑，在启动时将本地 `config.json` 中的所有配置项（包括 API Key）加载并合并到 `os.environ` 中，实现配置的单机跨会话持久化，避免用户重复填写 API Key。
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
  - `prototype/app/static/home.html` & `home.css` & `home.js`：根据用户要求，已将首页设计和内容完全替换为用户在 `ui/homepage` 分支中自行制作的版本。在此基础上，针对核心能力与试运行流程进行了精细化和去项目语境化优化：在六大能力面板中嵌入精美 SVG 图标与平滑微悬停动画；优化核心流程部分文案（去除了“不讲项目维护”等无关冗余词汇）；修改 Hero 栏访问方式为“App 本地安装测试”；重构操作指南 Step A 登录步骤为无需凭据的“启动并进入工作台”单机指导；移除了右下角会导致 Webview 导航丢失的“系统状态”外部链接；在 Footer 引入了统一的项目团队联系人信息（BD PS CE PMT 刘畅及邮箱）。
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
  - **安全性与可用性升级**：优化 Qdrant 启动检查为真实的 HTTP 接口轮询健康检测（10s 超时），确保彻底启动后再绑定端口；API 密钥读取路径实现 `环境变量优先 > 本地 config.json 次之 > 混淆密文兜底`；清空了 `app_desktop.py` 中硬编码的 `OB_DASHSCOPE_API_KEY` 以确保安全分发；配置 `.gitignore` 过滤 `prototype/bin/` 文件夹防止仓库污染；系统性清理了 `AGENTS.md`、`track_registry.json`、`docs/*`·、`prototype/README.md` 以及单元测试代码 (`prototype/tests/`) 中残留的 `New project` 历史路径。
  - **配置优先级、持久化保存与测试隔离优化 (2026-05-27)**:
    - **保存防误删**：重构了 `_set_runtime_config_value` 逻辑，空值以 `""` 保留于 overrides 中以确保覆盖生效；重构 `_save_runtime_config_to_file()` 移除了物理对比，仅在 overrides 中对应 key 显式为 `""` 时才从 `config.json` 抹除该配置，且保留了桌面端在环境变量中已经注入的持久化 API Key，实现安全的局部同步。
    - **清空配置会话即时生效**：空值 override 在当前会话下获取配置值时会直接截断并返回 `""`，不再错误 fallback 回 `os.getenv`，实现了会话态与持久化状态的 100% 对齐。
    - **测试隔离修复**：在 `test_runtime_readiness.py` 的测试用例环境中显式补齐了被 `clear=True` 误伤清除的 `REQPILOT_AI_TRACE_DIR` 环境变量，避免测试泄露污染真实存储引起 503 报错，测试完全隔离。
    - **无空格与全绿规范**：全面清理 `ai_copilot.py` 中多余的行尾空格，`git diff --check` 通过。单元测试全绿通过且打包成功。

## 验证状态

- Focused tests:
  - `PYTHONPATH=prototype python3 -m unittest discover -s prototype/tests -p "test_*.py" -q` -> `452 tests / OK` (全绿通过)
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
