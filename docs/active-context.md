# Active Context

更新时间：2026-07-15 CST
适用仓库：`/Users/liuchang/Documents/ReqPilotX`

长期背景：[`/Users/liuchang/Documents/ReqPilotX/docs/项目.md`](/Users/liuchang/Documents/ReqPilotX/docs/项目.md)

## 当前目标

- 需求分析模块进入 Azure OpenAI strict path 优化：Azure `llm_primary`/`llm_shadow` 运行时不能再被 heuristic fallback 静默接管。
- Azure retrieval backend 配置缺失时要 fail closed，暴露明确错误，而不是自动退回本地 heuristic evidence。
- 已完成 IRPS 产品真实语料的 RAG baseline：先完成本地结构盘点和 SOR/知识库分流，再以隔离 Azure 搜索索引生命周期采集 gold / hard-negative score 分布；每次运行均使用 run-specific source-doc ID 并清理 Azure Search chunks。
- IRPS evaluator 已新增纯本地 corpus preflight：真实语料必须先通过文件存在性、SHA-256 duplicate、ingestion 索引边界与 source-root containment 校验，才允许加载 Azure profile 或上传。
- 收口桌面端真实 SOR 上传后，后台 extraction preview 因 surrogate 字符导致的预览缓存失败。
- 保持便携包/桌面端对真实 PDF 的上传、预览与导出主链稳定，不把已修好的上传落库问题重新带回。

## 活跃分支 / 工作树

- 主线：`main` -> `/Users/liuchang/Documents/ReqPilotX`
- 当前热修直接在 `main` 工作树完成；`codex/ui-optimization` 保留但不是本轮焦点。

## 当前样本 / 保留工件

- `prototype/eval/extraction_diagnose/j02_311_20260516_fix28`
- `prototype/eval/extraction_batches/seres_mda_20260516_fix28`
- `prototype/eval/extraction_batches/legacy_benchmark_core_20260516_fix28`
- `prototype/docs/seres-mda-rule-fix-context.md`

## 最近已修

- **IRPS RAG corpus preflight (2026-07-15)**：
  - `prototype/scripts/irps_rag_eval.py` 新增 `--preflight-only` 与 `run_corpus_preflight()`；其只读取 manifest 和本机 source root，输出 `irps_rag_corpus_preflight.json`，不加载 Azure 配置、不解析文档、不写 Azure Search。
  - preflight 为每份 manifest 文件记录存在性、size、SHA-256、索引资格与 duplicate 验证；`searchable_knowledge` / `searchable_hard_negative` 是唯一 indexable ingestion，`do_not_index` / `visual_eval_only` 保持排除。
  - normal evaluation 在 `load_ai_copilot_config("azure")` 之前执行 preflight 并 fail closed；缺文件、未知/不一致 duplicate、未知 ingestion 与 `relative_path` 越出 source root 均会阻断 Azure 调用。
  - 真实 source root preflight 已通过：11 份文件均存在，唯一 duplicate 为 Application Note 与 Specification 的同名 AE PDF；两份 PDF SHA-256 一致。manifest 现将 Application Note PDF 指向 Specification PDF，保留 `iRPS_Product_Information_revAE.docx` 作为唯一可检索产品资料。
  - 首次真实 Azure 评测发现 evaluator 将解析文本只写入 `content`，而共享 `build_knowledge_chunks()` 只消费 `extracted_text`，使同步在零 chunk 时仍返回 `READY` 且所有查询为空。评测载荷已同时填入 `extracted_text`；回归测试覆盖真实 chunk builder，避免再出现空索引假阳性。
  - 修复后的隔离 Azure baseline：3 份可检索文档、8 条用例，`recall_at_1 = recall_at_3 = mrr = 0.857143`，run `irps-rag-eval-7a3549067c9c4b2aab10293bf2e727f0` 已确认 cleanup 完成。`IRPS-DEEPAL-003` 的 5 V/4.5-5.5 V 数值仅见于 CSP31/TQ250 跨项目 Test Spec，不在产品 DOCX；其 current direct-gold 标签需要人工修正，不能作为产品知识检索回归。`IRPS-DEEPAL-008` 是预期 visual-only 的文本 RAG false positive，后续需要视觉/图纸路由保护。
  - `prototype/tests/test_irps_rag_eval.py` 当前覆盖 manifest duplicate、preflight scope/duplicate、CLI 离线报告、source-root escape、缺文件不加载 Azure profile、Azure profile 选择、非空 chunk 载荷和 retrieval metrics cleanup；`8 tests / OK`。

- **Azure RAG/AI strict path v2 (2026-07-09)**：
  - `prototype/app/services/ai_copilot.py`：Azure `llm_shadow` / `llm_primary` 现在使用 LLM guardrail reconciliation，不再让 `_apply_hard_assessment_rules()` 的关键词规则静默改判 LLM 输出；显式 heuristic mode 保留原硬规则路径。
  - Azure evidence 记录新增 `chunk_id` / `score_source`，LLM prompt 的候选证据新增 `evidence_slot` / `chunk_id` / `score_source`，证据引用校验可用 chunk/slot 匹配，减少 excerpt 改写导致的误 fallback。
  - `AzureSearchSDKIndexer.upsert_chunks()` 上传前会按 `source_doc_id` 查询并删除旧 Azure Search chunks，避免知识文档重建后 stale chunk 继续污染检索。
  - `prototype/scripts/ai_smoke_suite.py`：新增 `--profile azure` / `--allow-skip`，Azure runtime 未配置时写出 skipped summary；summary 暴露 profile/provider/retrieval_backend/skipped/skip_reason/heuristic_run_detected。
  - 新增 `docs/superpowers/specs/2026-07-09-azure-rag-ai-strict-v2-design.md` 与 `plan/upgrade-azure-rag-ai-strict-v2-1.md`，记录设计与执行计划。
  - 真实 Azure E2E smoke 已通过：`python scripts\ai_smoke_suite.py --profile azure --mode llm_primary --sync-knowledge` -> `6/6`，所有需证据 case 都返回 Azure Search evidence，且 `heuristic_run_detected=false`。
  - smoke 的 `--sync-knowledge` 为每次运行生成独立 `project_id` / `source_doc_id`，评估后即清理 Azure Search chunks；评估异常也会清理，避免共享索引残留测试数据。
  - 修正 Azure Search SDK 删除调用为 `delete_documents(documents=[{"chunk_id": ...}])`，使真实 stale-chunk 删除和 smoke cleanup 生效。
  - Azure 原生 `@search.score` 真实直接命中文档约为 `0.032`，已将 Azure prompt/guardrail 阈值与 Qdrant 阈值分离并校准为 `0.03`；prompt 也会随 backend 传入正确阈值。
  - IRPS 语料盘点：`1 customer SOR`（SOR/RFQ、技术需求表、合规矩阵）、`2 Test Spec`、`3 Application Note`、`4 Drawing`、`6. Specification`。已发现 Application Note 与 Specification 中的 `电感式电机转子位置传感器产品通用信息介绍_AE.pdf` SHA-256 完全相同，入库必须去重。

- **Azure OpenAI 需求分析 strict path (2026-07-09)**：
  - `prototype/tests/test_ai_copilot.py`：先补 3 个红灯测试，锁定 Azure `llm_primary` screening/assessment 异常时不能调用 heuristic screening/assessment，以及 Azure retrieval backend 缺配置时不能调用 `HeuristicEvidenceRetriever`。
  - `prototype/app/services/ai_copilot.py`：`llm_primary` screening 失败现在直接返回 `screening_failed`；assessment 失败直接返回 `analysis_failed`；`azure_sdk` / `azure_haystack` retrieval runtime 缺配置时抛出明确错误并由分析链路封装为 `analysis_failed`。
  - 仍保留显式 `assessment_mode="heuristic"` / `retrieval_backend="local"` 的规则路径；本次只切断 Azure LLM 链路里的静默 heuristic 接管。

- **SOR 预览缓存 surrogate 清洗补口 (2026-06-23)**：
  - 在 parser 出口清洗已经生效的前提下，继续对真实失败 PDF `OHT5 征求意见稿_202606.pdf` 复现并递归扫描 preview payload，确认新的 surrogate 来源位于 `root.complete_requirement_entries[667].table_refs[0].slice_rows[0][0]`，即 preview 内深层表格单元格文本，而不是 `parse_document_with_diagnostics()` 返回值。
  - `prototype/app/services/extraction_preview.py`：新增递归 preview payload 清洗，在 `build_extraction_preview()` 返回边界统一剥离所有字符串中的 surrogate code points，覆盖 `complete_requirement_entries`、`complete_requirement_entries_raw`、`table_refs.slice_rows`、`sections`、`table_assets` 等所有预览输出字段。
  - `prototype/tests/test_extraction_preview.py`：新增 `test_sanitize_preview_payload_strips_surrogates_from_nested_table_rows`，覆盖深层 `table_refs -> slice_rows` 嵌套字符串的 surrogate 清洗回归。
  - 这次修复直接封堵了两个故障面：`preview_cache.persist_preview_cache()` 的 UTF-8 写盘失败，以及 live preview/API 响应在 JSON 序列化阶段再次遇到 surrogate 的风险。

- **桌面端 PDF 上传误报与表格快照写入稳定性 (2026-06-23)**：
  - 排查 `%LOCALAPPDATA%\ReqPilotX\storage\app_desktop.log` 后确认：用户看到的“上传失败(500)”不能直接等同于上传接口失败；前端 `onUploadSor()` / `onUploadKnowledge()` 会在上传成功后继续 `load*Documents()` + `loadWorkspace()`，任一步异常都会被统一显示成“上传失败”。
  - `prototype/app/static/app.js`：将 SOR/知识文档上传拆成“上传成功”和“上传后刷新”两个阶段；如果后续刷新失败，界面现在会显示“已上传，但工作区/列表刷新失败...”，不再误报整次上传失败。
  - 现场日志中唯一明确的桌面端 500 根因位于 `prototype/app/services/table_assets.py`：`_write_png_atomically()` 使用固定 `.{target.name}.tmp` 临时文件名，遇到残留或只读的旧 tmp 文件时会在快照写入阶段触发 `PermissionError`，并把保存/刷新类请求打成 500。
  - `prototype/app/services/table_assets.py`：改为 `tempfile.mkstemp(...)` 生成唯一临时 PNG 文件，再执行原子替换，避免与陈旧 `*.tmp` 文件名冲突；异常时会清理当前临时文件。
  - `prototype/tests/test_table_assets.py`：新增 `test_write_png_atomically_ignores_stale_read_only_temp_file`，覆盖“存在只读残留 tmp 文件仍可成功写快照”的回归场景。
  - 用户再次复现 SOR 上传 500 后，日志确认新的直接根因位于 `prototype/app/db.py:add_sor_document()` 之前：`parse_document_with_diagnostics()` 为 `OHT5征求意见稿_202606.pdf` 产出含孤立 surrogate 的 `extracted_text`，SQLite 在 UTF-8 编码时抛出 `UnicodeEncodeError: surrogates not allowed`。
  - `prototype/app/services/parser.py`：新增统一的 parser 出口清洗，将 `text`、`segments[*].text`、`segments[*].source_anchor` 中的 surrogate code points 统一剥离，并在 `_normalize_line()` 入口再次兜底，避免任一 PDF 引擎产出的异常 Unicode 进入 DB/JSON 链路。
  - `prototype/tests/test_parser_merge.py`：新增 `test_parse_document_with_diagnostics_strips_surrogate_codepoints`，覆盖 parser 结果含 surrogate 字符时的清洗回归。

- **Windows 便携测试包分发方案 (2026-05-29)**：
  - `prototype/build_portable.py`：新增绿色便携目录构建脚本，生成 `prototype/dist/ReqPilotX-Portable`，包含应用代码、静态资源、Windows Qdrant、精简 Python 运行时与当前虚拟环境依赖，并生成双击启动脚本与中文分发说明。
  - `prototype/app_desktop.py`：便携模式下通过 `REQPILOT_PORTABLE_MODE=1` 运行，默认/脚本指定数据目录为 `%LOCALAPPDATA%\ReqPilotX\storage`，避免 SQLite、Qdrant 数据和日志落入 OneDrive 同步目录。
  - 2026-06-02 同事电脑复现 `Router.__init__() got an unexpected keyword argument 'on_startup'`：根因是便携 Python 默认把用户级 `AppData\Roaming\Python\Python313\site-packages` 加到包内依赖前后，可能加载到不兼容的 Starlette。已在便携构建中写入 `python._pth` / `python313._pth`，启动脚本清空 `PYTHONHOME/PYTHONPATH` 并设置 `PYTHONNOUSERSITE=1`，`app_desktop.py` 启动早期也会优先锁定包内依赖；`requirements.txt` 显式锁定 `starlette==0.47.3`。
  - 2026-06-03 桌面端 Compliance Matrix 导出体验补齐：导出接口在桌面模式下返回 `local_file_path` / `local_export_dir` / `reveal_url`，前端导出成功后明确显示文件已保存到 `%LOCALAPPDATA%\ReqPilotX\storage\exports\...`，并支持“打开导出目录”直接定位，而不再依赖浏览器下载提示。
  - Windows PyInstaller `ReqPilotX.exe` 已能构建，但在当前企业代码完整性策略下被拒绝执行，因此测试分发优先使用自带 Python 的便携文件夹/ZIP；已验证自带 Python、自带 Qdrant、`/api/health`、`/workspace` 与 Qdrant `/collections` 正常。

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

- 2026-07-09 Azure RAG/AI strict path v2：
  - TDD 红灯已确认：5 个新增 Azure strict v2 tests 起初分别因 LLM 被关键词 hard rule 降级、Azure evidence 缺 `chunk_id`/`score_source`、prompt 缺 evidence slot/chunk、schema 禁止 `chunk_id`、Azure indexer 未清 stale chunk 而失败。
  - `python -m unittest tests.test_ai_copilot.AICopilotTests.test_run_ai_copilot_assessment_azure_llm_primary_does_not_apply_keyword_hard_rule_override tests.test_ai_copilot.AICopilotTests.test_azure_search_item_to_evidence_exposes_chunk_identity_and_score_source tests.test_ai_copilot.AICopilotTests.test_build_llm_messages_includes_evidence_slot_and_chunk_id tests.test_ai_copilot.AICopilotTests.test_validate_evidence_references_allows_chunk_id_match_when_excerpt_is_rewritten tests.test_ai_copilot.AICopilotTests.test_azure_search_sdk_indexer_deletes_existing_chunks_for_source_doc_before_upload -v` -> OK。
  - `python -m unittest tests.test_ai_copilot -v` -> `56 tests / OK`。
  - `python -m unittest tests.test_ai_smoke_suite -v` -> `1 test / OK`。
  - `python -m py_compile app\services\ai_copilot.py scripts\ai_smoke_suite.py` -> OK。
  - 当前 runtime 权限层拒绝继续执行 shell/local commands（permission host 返回 `denied-interactively-by-user` malformed payload）；因此本轮 final `codegraph sync/status` 与 final `git diff --check` 尚未完成，需要权限恢复后补跑。
- 2026-07-14 Azure 真环境验证：
  - `python -m unittest tests.test_ai_copilot tests.test_ai_smoke_suite -v` -> `62 tests / OK`。
  - `python -m py_compile app\services\ai_copilot.py scripts\ai_smoke_suite.py` -> `OK`。
  - `python scripts\ai_smoke_suite.py --profile azure --mode llm_primary --sync-knowledge` -> `6/6`、退出码 `0`、无 heuristic run。

- 2026-07-09 Azure OpenAI strict path：
  - TDD 红灯已确认：新增 3 个 Azure no-heuristic tests 起初均因 heuristic fallback 被调用而失败。
  - `python -m unittest tests.test_ai_copilot.AICopilotTests.test_run_ai_copilot_assessment_llm_primary_fails_closed_when_screening_errors tests.test_ai_copilot.AICopilotTests.test_run_ai_copilot_assessment_llm_primary_fails_closed_when_assessment_errors tests.test_ai_copilot.AICopilotTests.test_run_ai_copilot_assessment_azure_retrieval_misconfig_does_not_use_heuristic_retrieval -v` -> OK。
  - `python -m unittest tests.test_ai_copilot -v` -> `51 tests / OK`。
  - `codegraph sync .` + `codegraph status .` -> index up to date (`77 files`, `3,928 nodes`, `12,061 edges`)。

- 2026-06-23 SOR preview surrogate 修复：
  - `python -m unittest tests.test_extraction_preview.ExtractionPreviewFallbackTests.test_sanitize_preview_payload_strips_surrogates_from_nested_table_rows -v` -> OK。
  - 使用真实失败 PDF `OHT5征求意见稿_202606.pdf` 直调 `build_extraction_preview()` 并递归扫描整份 payload -> `no surrogates in preview`；`json.dumps(preview, ensure_ascii=False).encode("utf-8")` -> OK。
  - `python -m unittest tests.test_extraction_preview -q` 在当前 Windows 环境只因 `test_default_table_snapshot_root_prefers_staged_symlink_parent` 触发 `WinError 1314`（符号链接权限不足）失败；排除该已知环境前提用例后，其余 `45 tests / OK`。

- 2026-06-23 桌面端上传/快照写入修复：
  - 真实用户数据核查：目标 SOR PDF 已实际落库，`sor_documents.preview_status=READY`，说明“上传失败”提示可能来自上传后的刷新阶段而不是上传接口本身。
  - 使用真实 PDF 重放 `upload -> list_sor_documents -> workspace -> extraction-preview` 最小时序，在干净临时存储下全部返回 `200`；`preview` 初次请求处于 `BUILDING` 也不会直接 500。
  - `python -m unittest tests.test_table_assets.TableAssetsParsingTest.test_write_png_atomically_ignores_stale_read_only_temp_file -v` -> OK。
  - `python -m unittest tests.test_table_assets -q` -> `138 tests / OK / skipped=21`。
  - VS Code diagnostics for `prototype/app/services/table_assets.py` and `prototype/app/static/app.js` -> no errors；`node --check prototype/app/static/app.js` -> OK。
  - 第二轮 SOR 上传现场核查：`uploads/` 里新增了失败 PDF 文件但 `sor_documents` 无对应新行，随后日志确认是 `UnicodeEncodeError`；对同一 PDF 的解析结果统计显示修复前有 `87` 个 surrogate 字符，修复后为 `0`。
  - `python -m unittest tests.test_parser_merge.ParserMergeHeuristicsTests.test_parse_document_with_diagnostics_strips_surrogate_codepoints -v` -> OK。
  - `python -m unittest tests.test_parser_merge -q` -> `7 tests / OK`。
  - 使用真实失败 PDF 直调 `upload_sor_document()` 并保留真实 `db.add_sor_document()`，修复后已成功落库。

- 2026-06-22 提交前检查：
  - `git diff --check` -> OK。
  - `python -m py_compile app_desktop.py build_desktop.py build_portable.py app/main.py` -> OK。
  - VS Code diagnostics for touched files -> no errors。
  - `python -m unittest discover -s tests -p "test_*.py" -q` -> `452 tests` run, only `test_default_table_snapshot_root_prefers_staged_symlink_parent` failed on Windows with `WinError 1314` symlink privilege requirement。
  - Filtered full suite excluding that Windows symlink privilege case -> `451 tests / OK / skipped=23`。
  - Portable build smoke -> `build_portable.py` OK; bundled Python isolation OK (`FastAPI 0.116.1`, `Starlette 0.47.3`, no user-site path); launcher and bundled Qdrant present。
  - FastAPI export smoke with temp storage -> health ready, Compliance Matrix `.xlsx` created, desktop `local_file_path` / `reveal_url` returned and reveal endpoint OK。

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

- IRPS corpus 已完成真实 preflight 与隔离 Azure baseline；当前分析 blocker 是人工复核 `IRPS-DEEPAL-003` 的 direct-gold 假设，并明确 visual-only/unsupported 条目进入文本 RAG 前的路由策略。
- Azure strict v2 已切断本轮发现的 heuristic fallback 与关键词 hard-rule 静默改判点；后续 blocker 是真实 Azure Search/OpenAI 配置下的端到端 smoke 尚未在当前 turn 执行。
- 本轮 surrogate 问题已从“上传落库失败”与“preview cache 写盘失败”两处收口；当前没有新的用户可见 OPEN blocker 挂在 `OHT5 征求意见稿_202606.pdf` 这条链路上。
- `tests.test_extraction_preview.test_default_table_snapshot_root_prefers_staged_symlink_parent` 仍受当前 Windows `symlink` 权限限制影响，属于环境前提，不是本轮代码回归。
- `J02.311` 当前没有新的用户可见 OPEN 项；剩余 focused residual 是 `parser_low_confidence_hotspot`。
- `J02.311` parser 风险页仍是 `1 / 11 / 12 / 13 / 70 / 89 / 105 / 108 / 112`，当前不等价于可见回归。
- `legacy_benchmark_core` 与 `oil-pump-benchmark` 仍是历史红线；当前目标是“不回退”，不是顺手改绿。
- `SERES` 整批仍有 `profile_unmatched = 9` 与 `parser_low_confidence_hotspot = 11` 的审计债务。

## 下一步

- 人工复核并修正 `IRPS-DEEPAL-003`：将 5 V 项目数值标为缺少本项目权威文本证据，或补入经批准的 Deepal 项目资料；不要将 CSP31/TQ250 内容当作 Deepal 产品事实。
- 设计并验证 visual-only/unsupported requirement 的路由：图纸洁净度类条目不得直接从通用文本 RAG 生成证据结论。随后重新运行 `python scripts\irps_rag_eval.py --source-root "<path>"`，审阅 Recall@k、MRR、wrong-evidence rate 和 score-source 分布。
- 用真实 Azure OpenAI + Azure Search 配置运行 `python scripts\ai_smoke_suite.py --profile azure --mode llm_primary --allow-skip`；当 Azure runtime 完整时确认 `screening -> retrieval -> assessment -> trace` 全链路无 heuristic fallback run。
- 基于真实 smoke trace 继续校准 Azure relevance thresholds；当前已拆出 Azure 命名阈值，但具体数值仍需真实样本评估。
- 权限恢复后补跑 `codegraph sync . && codegraph status .` 与 `git diff --check -- <touched files>`。
- 继续收敛 AI 运行 trace/错误提示，让配置缺失、网络错误和模型输出校验失败在 UI 上区分清楚。
- 在桌面便携包上用同一份 `OHT5 征求意见稿_202606.pdf` 做一次 live spot-check，确认上传后 `preview_status` 稳定进入 `READY`，且工作区预览不再报 UTF-8 surrogate 异常。
- 如果需要重新分发测试包，基于当前源码重建 `prototype/dist/ReqPilotX-Portable` 与 ZIP，确保这次 preview 修复进入便携版。
- 将 `extraction-quality-loop` 作为 J02.311 用户可见残留已收口状态处理，准备分支收尾/合并或 PR。
- 若后续重启 review 服务，可做一次 `表11 / 表14 / 表12 / 图6图7 / 附录A` live spot-check，但不再阻塞 fix28。

## 活跃端口

- 本轮未重启前端或后端服务。
- 如需前端复核，先确认 review 服务状态并重新 refresh preview。
