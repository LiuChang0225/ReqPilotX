# SOR AI Copilot

面向汽车行业 SOR（Statement of Requirements）的需求分析平台。  
目标是把工程师从大量低增值“拆解/抄录”工作中解放出来，让团队把精力集中在真正增值的技术判断与客户沟通上。

当前实现态、系统架构图、优化计划与并行开发分支说明见：`/Users/liuchang/Documents/New project/docs/项目.md`

---

## 1. 产品定位（宣讲版）

### 1.1 我们在解决什么问题
在传统流程中，SOR 处理通常有三个痛点：
1. 需要人工逐条拆解 PDF/Word 到 Excel（耗时、重复、易漏项）。
2. 文档结构复杂（目录、章节、子章节、表格、水印/页眉页脚干扰），拆解质量不稳定。
3. 经验判断分散在个人知识里，难以沉淀为可复用能力。

### 1.2 我们的产品目标
- 第一目标：**条目拆解准确**（尤其是章节/子章节完整提取）。
- 第二目标：构建“可追溯”的判定链路（需求 -> 证据 -> AI建议 -> 人工结论）。
- 第三目标：形成可复用的迭代机制（标注、回归、质量门禁、趋势看板）。

### 1.3 面向对象（关键）
- **客户使用（外部）**：主控台 `workspace`。
- **开发者使用（内部）**：
  - PDF 拆解审阅台（用于结构与拆解质量核查）。
  - 项目进度看板（用于迭代管理与里程碑追踪）。

---

## 2. 当前能力总览

### 2.1 客户侧能力（主控台）
- 创建项目与客户档案。
- 上传 SOR（PDF/DOCX/TXT）与知识库文档。
- 一键分析并生成 Compliance Matrix。
- AI 给出初判建议：`ACCEPT / REJECT / DEVIATION_ACCEPT / NEED_CLARIFICATION`。
- 工程师可逐条改判并记录原因。
- 支持批量判定模板（按类型/章节前缀/关键词过滤后批量落库）。
- 支持版本化导出（版本标签 + 与历史导出版本差异 Sheet）。
- 一键导出 Excel（合规矩阵）。

### 2.2 内部研发能力（开发者工具）
- 拆解审阅台：
  - 章节树（1 / 1.1 / 1.1.1）
  - 章节一致性检查（缺号/重号/层级异常）
  - 章节覆盖统计
  - 完整条目筛选与保留
  - 去噪与解析引擎分布可视化
- 项目进度看板：
  - 迭代趋势（split/parse）
  - `rN vs rN+1` 差异审阅（文档/章节/条目）
  - 错误闭环面板（错误标签 -> 建议动作）
  - 高风险样本队列（优先人工复核）
  - quality gate 指标
  - 计划项管理
  - 里程碑事件时间线

### 2.3 截至目前的数据快照（代码实况）
来源：`eval/iterations/phase0_iteration_board.csv` 与 `r32_text_only` quality gate
- 已记录迭代：33 轮（`smoke_r00` 到 `r32_text_only`）
- 最新轮次：`r32_text_only`
  - `split_accuracy_pct = 100.00`
  - `parse_coverage_pct = 100.00`
  - `pages_with_items = 95/100`
- 文字类 quality gate：`95.45`（`21/22`）

> 注：分数本身不是终点，核心是“可复现、可解释、可持续迭代”的质量体系。

---

## 3. 我们的优势（宣讲重点）

### 3.1 业务价值优势
- 把“拆解录入”自动化，缩短响应周期。
- 把“经验判断”流程化，降低人员依赖。
- 把“沟通争议”证据化，提升客户沟通效率。

### 3.2 技术路径优势
- 解析层采用多引擎融合（pypdf/pdfplumber/pymupdf4llm/PyMuPDF rawdict），不是单一提取器。
- 针对真实样本持续做水印/页眉页脚/噪声清洗强化。
- 拆解层聚焦章节完整性，而不是只做句子切分。
- 形成闭环：标注 -> 回归 -> 质量门禁 -> 进度看板。

### 3.3 工程化优势
- 前后端可运行原型，流程完整。
- API、脚本、评测数据结构清晰，方便后续企业化扩展。
- 人工复核点明确，可控风险，不盲目“全自动判定”。

---

## 4. 难点与我们的解法（宣讲重点）

### 难点 A：复杂 PDF 结构识别
- 目录、标题、正文、表格混排，扫描质量不稳定。
- **解法**：多引擎页级选优 + 结构化章节识别 + 表格/测量值误识别抑制。

### 难点 B：水印、页眉页脚污染正文
- 水印字符碎片会进入需求文本，影响拆解准确率。
- **解法**：重复元信息过滤 + span 级特征过滤 + 水印片段规则持续迭代。

### 难点 C：评估机制容易“看起来高分”
- 如果没有严格回归机制，分数会失真。
- **解法**：Phase0 标注体系 + structural quality gate + 迭代看板全程留痕。

---

## 5. 产品架构（代码对照）

### 5.1 后端入口与路由
文件：`app/main.py`
- `GET /`：项目首页（宣讲与统一入口）
- `GET /workspace`：客户主控台
- `GET /extraction-review`：开发者拆解审阅台
- `GET /project-progress`：开发者进度看板
- `GET /phase0-review`：开发者标注台
- `GET /api/iterations/*`：迭代差异、错误闭环、高风险队列接口
- `POST /api/projects/{project_id}/requirements/batch-decision`：批量判定
- `POST /api/projects/{project_id}/export`：版本化导出（可对比）
- `GET /api/projects/{project_id}/exports`：导出版本列表

### 5.2 核心服务层
- 解析层：`app/services/parser.py`
- 拆解层：`app/services/extractor.py`
- 预览聚合：`app/services/extraction_preview.py`
- 预览缓存：`app/services/preview_cache.py`
- 表格资产：`app/services/table_assets.py`
- 检索：`app/services/rag.py`
- 判定：`app/services/assessor.py`
- 导出：`app/services/exporter.py`
- 进度追踪：`app/services/progress_tracker.py`

### 5.3 数据层
文件：`app/db.py`（SQLite）
- `projects / sor_documents / knowledge_documents`
- `requirements / assessments / evidences`
- `review_logs / export_jobs`

---

## 6. 页面与受众分层

### 6.1 项目首页（宣讲页）
- URL：`http://127.0.0.1:8010/`
- 作用：展示产品价值、优势、目标、技术路径与实时指标。

### 6.2 客户主控台（对外）
- URL：`http://127.0.0.1:8010/workspace`
- 作用：客户日常使用，上传-分析-复核-导出。

### 6.3 开发者工具（对内）
- 拆解审阅台：`http://127.0.0.1:8010/extraction-review`
- 进度看板：`http://127.0.0.1:8010/project-progress`
- 标注台：`http://127.0.0.1:8010/phase0-review`

---

## 7. 运行方式

```bash
cd /Users/liuchang/Documents/New\ project/prototype
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn app.main:app --reload --port 8010
```

---

## 8. 迭代与评测主命令

## 8.1 Git 协作约定
- 项目级 Git / GitHub / Codex 多窗口协作规范见：`/Users/liuchang/Documents/New project/docs/codex-git-workflow.md`

### 8.2 准备 Phase0 数据
```bash
PYTHONPATH=. .venv/bin/python scripts/phase0_prepare.py "/Users/liuchang/Downloads/Test pdf" \
  --target-pages 100 \
  --out-dir eval/phase0
```

### 8.3 生成基线报告
```bash
PYTHONPATH=. .venv/bin/python scripts/phase0_baseline_report.py \
  --phase0-dir eval/phase0
```

### 8.4 运行一轮迭代（建议文字类先行）
```bash
PYTHONPATH=. .venv/bin/python scripts/phase0_iteration_round.py \
  "/Users/liuchang/Downloads/Test pdf" \
  --round-id r33_text_only \
  --out-root eval/iterations \
  --target-pages 100 \
  --run-structural-gate \
  --exclude-name-regex "PRINT_21033420"
```

### 8.5 指定文档做章节回归
```bash
PYTHONPATH=. .venv/bin/python scripts/section_split_regression.py \
  "/Users/liuchang/Downloads/Test pdf/Q-JLY J7111029E-2024-汽车电气和电子零部件通用技术规范.pdf" \
  --json-out eval/iterations/r33_text_only/section_split_regression.json \
  --csv-out eval/iterations/r33_text_only/section_split_regression.csv
```

### 8.6 运行保真回归（推荐，替代旧人工标注依赖）
```bash
PYTHONPATH=. .venv/bin/python scripts/fidelity_benchmark.py \
  "/Users/liuchang/Downloads/Test pdf" \
  --profile-config eval/fidelity_profiles.json \
  --out-dir eval/fidelity/latest \
  --exclude-name-regex "PRINT_21033420"
```

输出文件：
- `eval/fidelity/latest/fidelity_docs.csv`
- `eval/fidelity/latest/fidelity_issues.csv`
- `eval/fidelity/latest/fidelity_summary.json`
- `eval/fidelity/latest/fidelity_summary.md`

### 8.7 一键回归套件（质量门禁 + 保真回归）
```bash
./run_regression.sh "/Users/liuchang/Downloads/Test pdf"
```

默认行为：
- 自动排除扫描件样本 `PRINT_21033420`（聚焦文字类拆解准确率）。
- 结构质量硬门禁默认开启：
  - `min_quality_score_pct = 95`
  - `min_quality_doc_score_pct = 70`
  - `max_quality_high_severity_issues = 0`

可选参数示例（不过滤扫描件）：
```bash
./run_regression.sh "/Users/liuchang/Downloads/Test pdf" --no-default-text-only-exclude
```

输出目录：
- `eval/regression_suite/<run_id>/regression_suite_summary.md`
- `eval/regression_suite/<run_id>/quality_gate/*`
- `eval/regression_suite/<run_id>/fidelity/*`

看板同步：
- `project-progress` 页面会自动读取 `eval/regression_suite/*/regression_suite_summary.json`
- 无需手动写入，运行回归后刷新看板即可看到最新回归门禁状态与历史记录

### 8.7 P0/P1 执行清单
- 文档：`docs/p0_p1_execution_backlog.md`
- 看板同步：`eval/progress/progress_tracker.json`

---

## 9. 技术栈

- Backend：FastAPI
- DB：SQLite
- Frontend：原生 HTML/CSS/JavaScript
- Parser：pypdf / pdfplumber / pymupdf / pymupdf4llm
- Export：xlsxwriter + Pillow

---

## 10. 下一阶段目标（Roadmap）

### P0（当前主线）
- `codex/table-assets-mainline`
  - 表格资产化主线：截图优先、Web/Excel 同源展示、`table_asset / table_ref`
- `codex/workspace-stabilization`
  - 统一工作台稳定性、性能、虚拟滚动、锁定/导出链路一致性

### P1
- `codex/ai-slice-integration`
  - AI 输入切换到 `slice_ai_projection + requirement 正文 + section context`
- 质量门禁分档（按文档类型/项目类型差异化阈值）

### P2
- 引入更强 OCR 管线，系统支持扫描件质量提升
- 检索升级为向量检索 + reranker
- 判定升级为 LLM 驱动，并保持可追溯审计

---

## 11. 目录提示

- 前端页面：`app/static/`
- 核心服务：`app/services/`
- 迭代数据：`eval/iterations/`
- 进度追踪：`eval/progress/progress_tracker.json`
- 导出目录：`storage/exports/`

---

## 12. 视觉对标说明（Apple 风格）

本项目首页与工作台视觉，当前按 Apple iPhone 首页的设计语言做了对标收敛，重点落在：
1. 大字号标题 + 短文案表达，减少信息噪声。
2. 高留白与分段节奏，突出“先价值、后细节”的阅读路径。
3. 低饱和中性色（浅灰/白）作为主背景，蓝色作为主要交互强调色。
4. 卡片边界与阴影克制化，避免花哨渐变造成“演示感过强”。
5. 客户入口与开发者入口分层，减少受众混淆。
