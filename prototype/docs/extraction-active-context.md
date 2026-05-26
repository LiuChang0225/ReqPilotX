# Extraction Active Context

更新时间：2026-04-24 13:26 CST
分支：`codex/extraction-quality-loop`

## 分支目的

- 当前分支仍是 extraction 回归修复入口；`fix25` 本轮只收敛诊断噪音，不改变用户可见 preview 主逻辑。
- `J02.311` 的 `complex_table_clause_binding_drift` 已确认是 tolerated requirement drift 被过度报警；以后只有超出 fidelity profile 容许范围、或有表格行/重复作用域证据时才报。
- `7.2 / 7.3 表10` 继续按 benchmark-safe accepted `snapshot_only` fallback 处理，不为单样本强推结构化。
- `2026-04-24` 用户反馈确认 `J02.311` 仍有多项用户可见 `OPEN` 问题；SERES 台账已新增专门问题清单，不能再称为已收口。

## 当前样本

- `Q-SK J02.311-2025_UDS on IP、CAN、CANFD诊断技术规范.pdf`
- `Q-JLYJ7111029E-2024-汽车电气和电子零部件通用技术规范.pdf`
- `SOR-N82KS-E01项目-T00007918-氮氧传感器-签批.pdf`
- `产品设计规格说明书PDS-DE全主动减振器总成-V1.0-20251127.pdf`
- `产品设计规格说明书PDS-DE前后油泵总成-V1.1-20251215.pdf`

## 最近已修

- `prototype/scripts/extraction_diagnose.py`
  - baseline delta 增加 fidelity profile 容许范围判断。
  - tolerated `requirements_drift = -1` 不再单凭 complex-table zone 生成 `complex_table_clause_binding_drift`。
  - 新增测试覆盖 tolerated drift 不报警、超出容许范围或存在 table evidence 时继续报警。
- `prototype/app/services/extraction_preview.py`
  - semantic visual rect 收集器可安全忽略 test/mock page 返回的非 dict/list 值。
- `prototype/tests/test_extractor_sections.py`
  - 附录 marker 期望已对齐当前实现：extractor 会保留 explicit `Appendix A` marker block。
- `fix24` 的部分行为仍在：alpha 条目合并到父行、`7.5.5 表12` 重复 raw pipe table 部分缓解、`Appendix A` heading 已进入 review entries。
- 但这些不等于用户可见问题已修完：nested list 层级、图/表 crop、附录 A 表 A.1 开头仍是 `OPEN`。
- 长期规则台账保留在 `prototype/docs/seres-mda-rule-fix-context.md`，不再把长账本塞回 active-context。
- **本地单机打包与 100% 离线运行 (Pywebview + PyInstaller + Native Qdrant)**:
  - 新增 `prototype/app_desktop.py`（桌面 GUI 启动器），后台线程拉起 FastAPI，并拉起本地预编译 of Qdrant 数据库（不依赖 Docker）。前端在 Pywebview 原生窗口渲染，数据持久化自动重定向到用户主目录。
  - 新增 `prototype/build_desktop.py`（跨平台打包脚本），一键打包静态资源、排除无用重型 ML 库（将体积优化至 301MB），并打包 macOS 和 Windows 的本地 Qdrant 预编译二进制程序。
  - **Uvicorn 日志流与时区兼容修复**：实现 `DesktopLogger` 属性动态委派避免 Uvicorn 报错；通过动态猴子补丁 `SafeDatetime` 绕过主代码库中 `progress_tracker` 时区 naive/aware 减法报错，且对原有后端没有任何侵入性。
  - **安全性与可用性升级**：优化 Qdrant 启动检查为真实的 HTTP 接口轮询健康检测（10s 超时），确保彻底启动后再绑定端口；API 密钥读取路径实现 `环境变量优先 > 本地 config.json 次之 > 混淆密文兜底`；清空了 `app_desktop.py` 中硬编码的 `OB_DASHSCOPE_API_KEY` 以确保安全分发；配置 `.gitignore` 过滤 `prototype/bin/` 文件夹防止仓库污染；系统性清理了 `AGENTS.md`、`track_registry.json`、`docs/*`、`prototype/README.md` 以及单元测试代码 (`prototype/tests/`) 中残留的 `New project` 历史路径。

## 当前保留工件

- `prototype/eval/extraction_diagnose/j02_311_20260424_fix25`
- `prototype/eval/extraction_batches/seres_mda_20260424_fix25`
- `prototype/eval/extraction_batches/legacy_benchmark_core_20260424_fix25`

## 验证状态

- Focused tests:
  - `python3 -m unittest prototype.tests.test_extraction_diagnose -q` -> `8 tests / OK`
  - `python3 -m unittest prototype.tests.test_extraction_preview -q` -> `52 tests / OK`
  - `python3 -m unittest prototype.tests.test_extractor_sections -q` -> `69 tests / OK`
- Focused diagnose:
  - `j02_311_20260424_fix25` -> `1` finding: `parser_low_confidence_hotspot` only。
  - `complex_table_clause_binding_drift` 已从 `J02.311` focused residual 中移除。
- Batch:
  - `seres_mda_20260424_fix25` -> `batch-report PASS`，`regression-suite PASS`，`quality 99/102`，`fidelity 72/72`；整批诊断剩 `parser_low_confidence_hotspot 11 + profile_unmatched 9`。
  - `legacy_benchmark_core_20260424_fix25` -> `quality 38/40`，`fidelity 80/96`；`regression-suite` 与 `oil-pump-benchmark` 仍为历史 FAIL，无新回退。

## 当前已知问题

- `J02.311`：parser 风险页仍是 `1 / 11 / 12 / 13 / 70 / 89 / 105 / 108 / 112`。
- `J02.311`：`1 范围`、`6.2.2`、`6.6.1` 的 alpha/numeric 嵌套层级仍不对；`1）/2）` 等内容被拆成独立数字行。
- `J02.311`：`图1 / 图8 / 图9 / 图10` 等显式图 crop 错切，已抽查 `图1` 仅截 caption，`图9` 截到大量正文。
- `J02.311`：`5.14` 疑似 false table；用户原话 `5.1.4` 需继续定位。
- `J02.311`：`7.5.2 表11` 跨页截图不全，`7.5.5` MD 表格渲染未闭环，`7.5.6.1 表14` 用户可见验收未通过。
- `J02.311`：`附录 A / 表A.1` 开头内容缺失，当前 live preview 从错误的 `01-0F` 位置开始。
- `J02.311`：`7.2 / 7.3 表10` 仍是 accepted `snapshot_only` fallback，未升成结构表。
- `legacy_benchmark_core` 与 `oil-pump-benchmark` 仍是历史红线；当前目标是 no-regression。

## 下一步

- 先修 `J02.311` nested alpha/numeric hierarchy，避免只把 alpha 合并进父行却丢掉子数字项归属。
- 再修显式 figure crop 选图逻辑，优先 `图1 / 图8 / 图9 / 图10`。
- 然后修 `附录 A / 表A.1` 起始边界与 `7.5.2 / 7.5.6.1` 表格可见验收。
