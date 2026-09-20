# Office 原生来源定位与程序验证

当前按用户“先把 PDF 支持搞定”的指令暂停 Office 扩展。保留已完成源码和下述程序证据；不继续新增 Office 功能/验证，也不把这些工作作为 PDF 正式候选验收前置。临时开发 bundle 不作为正式发布包。

本轮为 DOCX/XLSX 补原格式链路，不把转换后的 PDF 当客户原件。旧 PDF 的字段、省略规则和原文顺序分支保持兼容。Excel 由内部程序直接读取已验证工件导出；以下均是程序和开发验证，没有新增模型读 Excel 阶段。

## 来源合同

- Office `DocumentMapArtifactV1.source_format` 为 `docx` 或 `xlsx`；旧 PDF 省略该字段。
- Map 内容/锚点、表格片段/单元格、SRC 文字/表格/图片绑定带 `native_locator`、`source_sequence`。DOCX 保留正文对象、段落、表/行/列/对象索引；XLSX 保留 workbook、sheet、cell_range、对象索引。原生索引沿 SourceIR 使用零基；人可读标签才转一基。
- 未知页和几何保持 `page=null`、`bbox=null`、`source_bounds=null`、`pages=[]`。不分配假页、不改原文件 SHA。
- 编译器重捕原件验证 SourceIR 字段。后续导入、导出和 review 核验复查真实原件中的文本、表格、合并关系、定位和图片资产，避免仅信任可重新计算的 JSON 哈希。
- 同表/同 sheet 的多个条目用真实行、列、范围及文字 offset 排序。DOCX 表内段落只有在原单元格的文本和位置可复核时才作为 TableIR 的重复捕获别名保留，避免双重 SRC；表内图片另行保留。
- DOCX 真实单元格内嵌图片可归属已有表格 SRC。XLSX 浮动绘图的单元格锚点本身不证明图片属于该表格数据，不自动等同于单元格图片。

主要实现：`source_locations.py`、`office_source_evidence.py`，共享 Map/baseline/ledger/decomposition/visual/delivery 模块，以及 portable `compile-map`、`compile-items`、`image-packet`、`deliver` 和 review 原件复核。

## 原文只读接口

`GET /api/projects/{project_id}/sor-documents/{document_id}/source-capture/native-view?source_sha256=...`

返回 `reqpilot-native-source-view/v1`：`source_sha256`、`source_format`、`source_file_name`、`status`、`authority=ORIGINAL_SOURCE_NATIVE_CAPTURE`、`nodes`、`diagnostics`。每个 node 包含真实 `source_node_id`、`parent_source_node_id`、`kind`、`source_sequence`、`native_locator`、`text`、可选完整 TableIR `table` 和 `image`。TableIR 单元格使用 `raw_text`、`row/column/row_span/column_span`；工作台 table-preview 使用 `text`。图片的 `pixel_width/pixel_height` 未预报时为 null；图片响应本身仍是原件真实像素。

`GET /api/projects/{project_id}/sor-documents/{document_id}/source-capture/native-objects/{source_node_id}?source_sha256=...&content_sha256=...`

返回原始图片 bytes，按已登记项目/文档/源 SHA 核验，再从原件实际对象和资产对应取图。调用者不能提交任意磁盘路径、bbox 或替代图片。非 raster/不可读取对象明确失败。两个接口只读，不发布 Map、不创建 Agent 任务。

## 空模板表

`SourceRequirementSliceCandidateV1.records` 允许空数组，但仍须完整覆盖原始表行和单元格。非 HEADER context 不能含未说明正文。需要把原件中的字面占位符当未填模板时，在对应 context 行显式提供 `exact_placeholder_values` 与 `empty_template_reason`；每个声明值必须真实出现，每个非空格必须逐值匹配。程序不默认把 `/`、`0`、`N/A` 当空值。该声明属于可审查的 Agent 语义判断，不证明没有义务。

零记录后继必须保持已有 HEADER 行边界；不能把旧正文全部改 HEADER 绕过验证。占位声明随 context 工件、scope template 和 Map 重放保留，原表网格与所有原始占位值原样导出。旧缺省字段省略，不改变旧有效工件哈希。

## 已有程序证据

私有证据根：`prototype/runtime-data/agent-loop-20260908/office-native-slice/`。

- `verification.json`：自包含开发 bundle 在 macOS 的精简 Python 环境、`PATH=/usr/bin:/bin`（无 Codex）运行真实 CLI。DOCX 10 次调用：4 SRC，必要文字派生后 4 个分析单元；XLSX 8 次调用：同 sheet 两区域、第二 sheet、5 SRC/4 分析单元。原文件 SHA 保持，图片原字节进入 ZIP，公式缓存与内链核对。内部资料未提供，分析显式待证据。
- `bundled-v2/{docx,xlsx}/native-source-view.json`、`original-image.png`：后端只读原生视图及实际对象 bytes 的合成样例，供前端消费。这里的 `bundled-v2` 是测试目录名，不是正式插件版本。
- `pdf-compatibility.json`：PDS/E01 旧 Map/基线 JSON 与 hash 全等；E01 实际 Map、预览、请求重新编译全等，450 非图像原文绑定及顺序重放一致，图片绑定复验。PDS 实际文字 scope 重放一致；其已记录的旧表格 range/preview 不一致仍不被放宽，不宣称完整 legacy 表重放通过。
- `../empty-table-replay/verification.json`：以生产 Agent 实际已纠正表头的不可变后继作为只读前驱，对 table-25 做程序合同回放。显式 `/` 声明后 448→447，其他 447 条业务字段不变，水位 463 保持，10 原格/5 个 `/` 保留；旧候选和缺声明失败均保留，未改生产候选或执行模型。

源码测试覆盖：`test_office_source_pipeline.py`、`test_source_location_contract.py`、`test_empty_source_table.py`、`test_native_source_view.py`。覆盖原件/定位/单元格篡改拒绝、原图入包、原编号注解、Map 同源后继、合并单元格与原生顺序；共享旧 PDF 回归与插件测试另行执行。

## 边界与后续

这些是合成程序切片与只读 HTTP adapter 验证，不代替真实客户 Office 的语义复核、应用工作台端到端、其他 coding agent 或 Windows/Linux 宿主实测。前端原生阅读分支由主线同时接入；正式冻结/安装必须等待联合回归。旧 r11 及已安装中间版本不原地修改。

XLSB 不属于本轮原生全流程。现有可选 capture converter 依赖 Darwin、`/opt/homebrew/bin/soffice`（LibreOffice 26.2.4.2）与 `/usr/bin/sandbox-exec`；doctor 单独列其平台/文件可用性和未执行的版本验证，不以 Python READY 代替该转换器或完整业务验证。PDF/DOCX/XLSX 不依赖此转换器。

暂停时没有运行中 Office 命令。额外 review-chain 实验的 DOCX CLI close/verify 已返回，但其测试汇总器因读取省略的 `image_bindings` 字段报 KeyError，未形成该实验最终验证回执；原始 `review-chain/docx/calls.json` 保留，XLSX 后续未执行。该实验不计入已通过的正式验证，也不继续修跑。
