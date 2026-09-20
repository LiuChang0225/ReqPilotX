# PDF 叠印文字的阅读投影

更新：2026-09-07。修复的是原生 PDF 绘制重复进入阅读内容的问题；不改写 SourceIR、Document Map、SourceItem 基线或客户原件，也不改变分类、层级和 SRC/REQ。

## 已确认的原因与处理边界

部分 PDF 用同一字形在略有偏移的位置重复绘制来模拟粗体。原生 parser 会把四次绘制返回为四组文字，有时一组是完整行、其余是前后片段。保留原始绘制证据是正确的；直接把全部绘制次数作为可读原文展示则会重复。

`app.adapters.documents.pdf_text_reading_projection.project_pdf_source_contents(source_path, *, expected_source_sha256, contents)` 直接消费已有 `source_content` 字段：`content_id/page/bbox/text/text_sha256`。输入原文件先校验 SHA；内容必须精确匹配同页、同位置的一个原生完整 block。部分 block、多 block 或无法唯一绑定的内容暂不投影。

只有以下证据同时成立，才能从阅读视图折叠一条原生绘制行：该行全部字符在保留行中有逐字相等的对应；字体、字号、颜色、透明度及文字方向一致；字符 bbox 和 origin 坐标均相近。每坐标误差上限为 `min(0.35 PDF pt, 0.06 × 字号)`，小字容差随字号下降、大字容差不无限增加。缺少字符几何、透明隐藏文字、不同字体、不同方向或不同物理位置均不折叠。

优先保留完整原生行，不通过拼写或模型重新生成句子。保留行沿用原绘制顺序；这项操作不会决定章节或跨栏的最终阅读顺序。相互重叠的部分片段不能据此全部恢复；已经证明的整行可折叠，其余片段继续保留，并在 `remaining_partial_overlaps` 中明示。

## 原文和投影的合同

回执以 `content_id` 返回，包含 `display_text/display_text_sha256`、`raw_text_sha256`、原文 `source_ranges`、`duplicate_ranges`（移除绘制范围到保留范围的对应、字符几何证明摘要）、来源 SHA、页、block、bbox 与投影 SHA。

`text`、`raw_text`、原 line offsets 与原文哈希始终不变。展示或分析使用投影时必须同时保留原捕获入口及此回执；不能把更短的展示文本静默写回旧基线。在本轮接口中，SourceItem 只应对覆盖整个内容的 binding 使用该投影，局部 binding 不推测对应。

既有 `COLLAPSE_REPEATED_SOURCE_LINES` 操作现在调用同一几何证明。只有文本相邻相同或前后缀相等已经不足以接受此操作；原文件不可用时不作清洗。旧版纯文本折叠不能成为新自动化的依据。

阅读投影放在公开响应顶层的 `reading_projections`，不向 `document_tree` 增加或改写展示字段。原因是既有 Document Map 编译器会将整个公开树作为来源投影身份的一部分；即使只新增展示回执，也会使旧基线无法绑定。阅读视图与原文身份必须分离。历史公开树若带有旧的 `display_text`，只可在所有 typed Map 业务字段完全相同的前提下由既有版本解析器兼容，不能为新显示方式重签原基线。

## 本轮实证与限制

真实 PDS 的 101 个 Map 原文内容中，7 个有整行叠印证明；对应当前第 26 版的 5 条非需求 SourceItem 和 2 个标题。前件和后件的 14 行捕获各显示为 2 行，四个蓄能器的 32 行捕获显示为 5 行。依据标准信息仍存在部分重叠片段，未声称完整修复。主文 1.2 两个相距约 57 pt 的实际说明文件条目全部保留。

全 PDF 另有 20 个原生 block 检出整行叠印，含表头等并行表格捕获。这个数量不等于需求数，也不表示全部表格或全文已修复。原 449 条记录、原内容、原编号及基线对象 SHA 没有改变。

聚焦回归覆盖四次绘制及完整行/片段混排、异位置同文、部分重叠、缺失几何、不同字体/透明度/方向、容差尺度、真实旋转 PDF、来源/内容错绑拒绝、原文件和输入对象不变。原件 P1/P10/P16/P22/P23 已渲染检查。私有证据在 `prototype/runtime-data/pds-overprint-fix-20260907/`，包括原生 rawdict、图片、逐内容投影和与旧基线的影响对照；不提交客户资料。

```bash
PYTHONPATH=prototype prototype/.venv/bin/python -m unittest discover -s prototype/tests -p test_pdf_text_reading_projection.py -q
PYTHONPATH=prototype prototype/.venv/bin/python -m unittest discover -s prototype/tests -p test_native_structure_feedback_bridge.py -q
```
