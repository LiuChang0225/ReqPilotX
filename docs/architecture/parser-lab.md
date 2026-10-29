# Parser Lab 模块

Parser Lab 是 ReqPilotX 的本地解析诊断模块，用于查看解析器实际输出、SourceIR 映射和二次显示规则的差异。它复用现有业务事实模型，不创建 Document Map、SourceItem、正式需求基线或人工决定，不调用 Agent、云模型或自动下载模型。

## 模块边界与启动

| 层 | 路径 | 职责 |
|---|---|---|
| 共享 UI | `packages/workbench-ui/src/features/parser-lab/` | 三视图、双向定位、原位重建、原生/规则投影、页图缓存 |
| 运行存储 | `apps/server/src/reqpilotx_server/application/parser_lab.py` | 新建运行、原始证据、SourceIR、资产及 hash 索引 |
| HTTP API | `apps/server/src/reqpilotx_server/api/routers/parser_lab.py` | 解析器目录、上传、运行读取及已登记工件读取 |
| 独立入口 | `apps/server/src/reqpilotx_server/parser_lab.py` | 无数据库、worker、账户或业务 API 的 loopback 工具 |
| 解析映射 | `packages/engine/src/reqpilotx/adapters/documents/` | 复用 PyMuPDF adapter；独立 pdfplumber 与 Tesseract adapter |

复用仓库 Python/Node 环境，在仓库根执行：

```sh
npm run build
PYTHONPATH=packages/engine/src:apps/server/src prototype/.venv/bin/python -B \
  -m reqpilotx_server.parser_lab \
  --storage-root /tmp/reqpilotx-parser-lab \
  --frontend-dir apps/web/dist --port 8765
```

访问 `http://127.0.0.1:8765/parser-lab`，或用 `?run=<已有运行ID>`打开指定记录。CLI 固定绑定 `127.0.0.1`，验证 Host/Origin；无公网绑定参数、后台自启或系统服务。前台按 Ctrl+C 停止。后台运行时，先用 `ps -p <PID> -o pid=,command=` 确认模块、端口及运行根，再对该 PID 发送 SIGTERM。

独立入口通过只读 `/api/auth/session` 告知共享前端账户模式为关闭，不加载 SaaS 账户实现，也不提供登录/注册 API。主应用只在 SaaS 关闭且部署模式为 LOCAL/TEST 时挂载 Parser Lab API；CLOUD 或 SaaS 开启时均不挂载。

`--storage-root` 必须显式指定，使用私有运行目录。`/tmp` 可被系统清理；长期保存时选择已授权的持久目录。客户 PDF、raw、SourceIR、页图和资产都不能提交 Git。页面不包含演示运行，空目录即空状态。上传后逐次运行，可比较同一 source hash 的不同解析器记录。

## 证据与接口

- `GET /api/parser-lab/parsers`：实际版本、可用性和缺失依赖。
- `GET /api/parser-lab/runs`、`GET /api/parser-lab/runs/{id}`：运行回执及 SourceIR。
- `POST /api/parser-lab/runs`：multipart `file`、`parser`、`language`，新建独立运行。
- `GET /api/parser-lab/runs/{id}/artifacts/{artifact_id}`：仅允许读取已登记工件，校验 hash 并拒绝越界路径。

`SourceIRSnapshot`、`SourceNode`、`TableIR`、`SourceAssetRef` 和 `VisualTextSnapshot` 是唯一正规化事实模型。`parser-lab-run/v1` 只是运行回执和工件索引，不是第二套业务内容 schema。

每次调用生成新 UUID 目录，保留原 PDF、文件名/大小/SHA-256、开始结束时间、耗时、parser/adapter/映射版本、配置、preflight、实际原生输出、SourceIR 和真实图片资产。所有工件登记字节数与 SHA-256；bytes 在 raw JSON 中以 base64 无损保存。原始工件排他创建，无编辑接口；重新解析产生新记录，人工更正不能写回 raw。

页面证据保留 mediabox/cropbox、旋转、坐标变换矩阵、页面及渲染尺寸。UI 和 PyMuPDF SourceIR 共同使用 rotated page.rect 左上角 point 坐标系，不重复旋转。只有区域的图片不生成虚假资产，表格节点必须有实际 `table_payload`。

状态区分 RUNNING、COMPLETE、PARTIAL、FAILED、UNSUPPORTED、MISSING_DEPENDENCY，不以演示内容替代错误。输入上限 25 MiB、100 页，单页渲染最多 2000 万像素。进程内串行解析避免 PyMuPDF 表格检测全局状态互相污染。尚未实现后台任务队列、取消、崩溃恢复或大文件性能优化；中断可能留下 RUNNING 回执。

## 解析器范围

| 解析器 | 当前实际通路 | 明确边界 |
|---|---|---|
| PyMuPDF | 复用 `PdfSourceIRAdapter` 和 asset publisher；可选 observer 捕获映射过程中实际调用的 rawdict、drawings、bboxlog、find_tables | 未启用 observer 的原调用保持原行为；SourceIR 含既有 adapter 加工，并非原始 raw 本身 |
| pdfplumber | 原生 chars/text lines、表格矩阵及单元格映射；raw 保留 PDF stream | 旋转/裁剪页和合并网格映射未支持，明确 PARTIAL；图片只有区域时保留缺失诊断，不创建 IMAGE |
| Tesseract OCR | 实际本地 binary TSV、stdout/stderr、输入 PNG、binary/traineddata hash；独立 `visual_text_snapshots` | 只提取文字，不覆盖 native text，不声明表格或图片识别，状态 PARTIAL；缺语言包为 MISSING_DEPENDENCY；默认 eng，中文需已有 chi_sim |
| Docling | 目录明确 UNSUPPORTED，并独立报告是否安装依赖 | 本模块未接入或分发未验证 adapter 草稿；不自动安装依赖、下载模型或使用其他解析器冒充 |

## 可视化与二次规则

PDF 对照、内容对照、PDF＋内容三个模式可自由往返。PDF＋内容左侧展示当前单解析器的原页渲染及 bbox/order，右侧默认白底 SVG 原位画布，也可切为完整结构化全文列表。画布只使用实际提取的文字、行/块框、表格单元格及图片资产，不垫原页图。字号、换行及缺 cell bbox 时的拓扑位置属于明确标注的渲染推算，不是原生字体或坐标恢复。无 bbox 的内容仍保留在全文列表。

两侧共享页码、缩放及选择，表格单元格、画布、列表与 PDF 双向定位。翻页包括上/下页、页码跳转、PageUp/PageDown、左右键、Home/End、邻近五页缩略图。页图 GET 使用浏览器内存缓存，最多保留 16 个已加载条目并预加载相邻页；切页不重跑解析。切页/切运行隐藏旧内容并拒绝迟到响应。下次运行的解析器/OCR 配置与已保存运行分别显示。

八类稳定配色配合文字图例：正文、标题、表格、图片、页眉、页脚、疑似水印、未分类。无可靠原生语义类别的文字在原生层显示未分类。原生层仍是 SourceIR 映射视角，精确原生调用以 raw 工件为准，不能把 adapter 标签或顺序冒称解析器原生能力。

规则层仅是不可变输入上的显示投影，显示来源和理由：

- `pl-repeat-margin/v1`：跨页重复且位于顶部/底部 10% 的页眉/页脚候选，支持 Page n/m 与第 n 页共 m 页模板。
- `pl-large-repeated-centre/v1`：跨页重复、中央大幅短文本的疑似水印。
- `pl-heading-candidate/v1`：既有映射或行高等版面线索产生标题候选。
- `pl-geometric-order/v1`：按页/y/x 排序的几何展示顺序，不宣称多栏语义阅读顺序。
- `pl-table-text-overlap/v1`：文字块完全处在表格框内且匹配真实单元格文本时，仅在规则画布折叠重复文字；原生视图、完整列表及 raw 保留。

噪声开关仅隐藏候选，不删除原文。位图水印、倾斜文本和复杂布局未可靠判定；未标记不能解释为没有噪声。候选规则不构成业务分类或真实文档质量验收。

## 验证范围

本模块只做可视化和功能验证，不安装 OmniDocBench、不评分、不制作 GT。后端测试明确生成合成契约 PDF，实际调用本地 PyMuPDF、pdfplumber 和已有 Tesseract；前端使用构造传输/几何输入测试竞态、投影与交互，不能当作解析器原生能力证据。真实客户 PDF 质量验收仍待许可样本。

```sh
prototype/.venv/bin/python -B scripts/test_python.py backend --pattern test_parser_lab.py
prototype/.venv/bin/python -B scripts/test_python.py backend --pattern test_pdf_source_ir_adapter.py
prototype/.venv/bin/python -B scripts/test_python.py backend --pattern test_target_runtime_foundation.py
npm test -- --run packages/workbench-ui/src/features/parser-lab/ParserLabPage.test.tsx packages/workbench-ui/src/features/parser-lab/presentation.test.ts apps/web/src/app/App.test.tsx apps/web/src/auth/AccountGate.test.tsx
npm run build
```

验证包含 raw/hash/重复运行隔离、表格/资产、旋转裁剪、缺依赖/未支持/损坏输入、OCR 独立层、Host/Origin、CLI 不加载数据库、独立账户模式与 SaaS/CLOUD 隔离，以及三视图、快切页/运行、白底重建、双向定位、规则来源和原始数据不变。测试回执和运行工件留在本地，不随模块分发。

上传前在远端开发基线 `fe97563eb6eec6537f821fd840cbf5a367acee8b` 的隔离目录重新运行：前端四文件 35 项、Parser Lab 后端 12 项、PyMuPDF adapter 169 项、运行装配 72 项全部通过；`npm run build`（含 typecheck）和 `git diff --check` 通过。构建仍有既有主包超过 500 kB 的提示。实际浏览器打开该构建的独立入口，账户模式探测、静态资源、parser catalog 和空运行列表均正常；临时验证端口检查后已停止。这些结果仅证明程序功能，不代表真实文档质量验收。
