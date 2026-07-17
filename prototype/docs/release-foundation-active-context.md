# Release Foundation Active Context

更新时间：2026-06-23 14:36 CST
分支：`codex/release-foundation`

## 分支目的

- 收口试运行前的 P0 稳定性问题
- 接入 Entra 登录与内部试运行反馈骨架
- 让控制塔和真实运行底座对齐

## 当前关注

- control tower / trackctl / skill 已就位，等待该支线真正开工
- health / readiness / smoke 基线
- 账号系统接口与会话状态
- 反馈采集最小闭环
- 桌面端 PDF 上传后的刷新误报与表格快照原子写稳定性

## 最近已修

- 通过 `%LOCALAPPDATA%\ReqPilotX\storage\app_desktop.log` 定位到一个真实桌面端 500：`prototype/app/services/table_assets.py::_write_png_atomically()` 使用固定 `.{target.name}.tmp` 文件名，遇到残留或只读旧 tmp 文件时会抛 `PermissionError`。
- `prototype/app/services/table_assets.py` 已改为使用 `tempfile.mkstemp(...)` 生成唯一临时 PNG 文件，再原子替换目标快照；对应回归测试 `tests.test_table_assets.TableAssetsParsingTest.test_write_png_atomically_ignores_stale_read_only_temp_file` 已通过。
- `prototype/app/static/app.js` 已把 SOR/知识文档上传与上传后刷新拆开处理；后续若只是 `loadWorkspace()` 或 `loadKnowledgeDocuments()` 失败，界面会显示“已上传，但刷新失败”，不再把整次上传误报成上传失败。
- 用户再次确认是 SOR 上传本身 500 后，日志定位到第二个真实根因：`prototype/app/services/parser.py` 对特定 PDF 会输出含孤立 surrogate 的 `extracted_text`，在 `prototype/app/db.py:add_sor_document()` 写 SQLite 时触发 `UnicodeEncodeError: surrogates not allowed`。
- `prototype/app/services/parser.py` 已增加统一结果清洗，剥离 `text` 与 `segments` 中的 surrogate code points；`tests.test_parser_merge.ParserMergeHeuristicsTests.test_parse_document_with_diagnostics_strips_surrogate_codepoints` 已覆盖该回归，并且对用户现场失败 PDF 复测后 surrogate 数已从 `87` 归零。

## 当前 blocker

- 对应 worktree 还没创建，控制塔当前会把这条 active track 保持在 `TODO / ON_PLAN / not_started`
- 上传长任务、readiness 语义、SQLite 故障路径仍是既有技术债
- 认证与反馈还没有正式落地
- 当前仍未在用户现场直接复现“上传接口本身 500”；更像是上传后刷新失败的误报，需在下一次桌面端实机重试中确认。
 - 当前源码已解释并覆盖两类桌面端上传相关故障：上传后刷新误报，以及 parser surrogate 导致的 SQLite 编码失败；还需用户用新包实机确认。

## 下一步

- 创建 `codex/release-foundation` worktree
- 开工即运行 `python prototype/scripts/trackctl.py start --track release-foundation`
- 先推进 P0 稳定性、auth 和 feedback 骨架
- 使用当前修复后的代码/测试包重试一次桌面端 PDF 上传与条目保存，确认不再出现误报或 `table_snapshots/*.tmp` 写入冲突。
 - 用新构建的便携包重试用户现场失败的 `OHT5征求意见稿_202606.pdf` SOR 上传，优先确认 `sor_documents` 能新增记录且前端不再出现 500。
