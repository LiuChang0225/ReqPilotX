---
name: reqpilotx-wiki-compiler
description: Ingest automotive component technical assets (specifications, E/E schematics, software diagnostics, DVP&R test standards, drawings, integration rules) into structured, interlinked Markdown LLM Wikis (Karpathy paradigm). Supports adaptive content-driven domain partitioning, single-file incremental ingestion, multi-file folder batch compilation, graph linting, and Obsidian-like graph data export.
---

# ReqPilotX Wiki Compiler Skill (Automotive Domain Edition)

Own the compilation, incremental synthesis, topology verification, and graph data export of automotive engineering knowledge bases for ReqPilotX requirement analysis agents.

This skill transforms heterogeneous technical documents (PDF, DOCX, PPTX, Drawings, Schematics) into an atomic, interlinked, evidence-backed Markdown Wiki rather than chopping documents into fragmented, out-of-context vector chunks.

---

## 1. 汽车零部件 6 大核心领域本体 (Automotive Domain Ontology)

知识库组织严禁机械套用固态文件夹，而应基于文档**工程实质内容**自适应划分至 6 大标准维度（详见 `references/automotive_domain_ontology.md`）：

1. **`specs/` (系统规格与物理包线)**:
   - 额定/峰值工作转速、外形包络尺寸、工作温区工况谱、专有术语表。
2. **`ee_hardware/` (电子电气硬件与接口防护)**:
   - 供电拓扑 (5V/12V LDO)、反接/过压钳位保护 (TVS)、差分信号驱动、共模扼流滤波、车规级 ESD (ISO 10605)。
3. **`software_diagnostics/` (嵌入式软件与车载诊断)**:
   - 角度跟踪观测环路 (ATO/PLL)、幅值与正交度实时监控、故障安全状态机 (High-Z/安全电平)、SAE J2716 SENT 协议、ISO 14229 UDS 故障码 (DTC)。
4. **`dvp_tests/` (试验规程与工况验证 DVP&R)**:
   - 高温热冲击、耐高温热油耐久、三轴宽频随机振动 (ISO 19453-3)、EMC 大电流注入 (BCI) 与杂散磁场抗扰。
5. **`interfaces/` (机械几何、引脚与接插件)**:
   - PCD 安装孔位、装配紧固力矩 (Nm)、Pinout 针脚分配、车规连接器与 FLR13Y 耐温导线。
6. **`integration_rules/` (系统集成、公差与偏离准则)**:
   - 气隙与同轴度公差链叠加 (RSS 均方根法)、倾斜偏角敏感度、客户 SOR 技术偏离 (Deviation) 处置 SOP。

---

## 2. 核心能力与执行模式 (Execution Modes)

### 模式 A: 基于内容的自适应批量编译 (`INGEST_FOLDER`)
当面对整套零部件工程资料文件夹时：
1. **扫描清单**：运行 `wiki_compiler.py scan --source <dir> --output sources_manifest.json`。
2. **自适应内容分析**：
   - 包含原理图/供电/Pinout -> 映射至 `ee_hardware/` 与 `interfaces/`；
   - 包含算法/解调/通信协议/DTC -> 映射至 `software_diagnostics/`；
   - 包含试验用例/振动图谱/温变曲线 -> 映射至 `dvp_tests/`；
   - 包含公差叠加/偏角配合 -> 映射至 `integration_rules/`；
   - 包含系统包线/定义 -> 映射至 `specs/`。
3. **原子化撰写**：遵循 `references/page_schema.md` 模板，必须附带标称参数表与原始文档锚点 (`source_evidence`)。
4. **拓扑编织与验证**：生成 `index.md`，执行 `wiki_compiler.py lint` 确保 0 悬空链接、0 孤岛节点。

### 模式 B: 单文档增量融合 (`INGEST_FILE`)
当收到技术协议增补、图纸修版或新试验报告时：
1. 解析文本段落与参数表，检索现有图谱是否有对应概念。
2. **已存在概念**（如工作温度上限调整）：生成局部差异 Patch，更新参数表并在 Evidence Anchors 追加新版本来源。
3. **新增概念**：自适应选定合适领域分类创建新节点，并与上下游节点双向双链链接 (`[[node_id]]`)。

### 模式 C: 图谱拓扑质量门禁 (`LINT_WIKI`)
严格的质量检查流水线：
```bash
python3 scripts/wiki_compiler.py lint --wiki "/absolute/private/wiki"
```
- **0 Broken Links**：所有 `[[wikilinks]]` 目标必须物理存在；
- **0 Orphan Pages**：除 `index.md` 外，每个词条必须有至少一条入链；
- **冲突检测**：运行 `wiki_conflict_detector.py` 自动比对不同文档间的数值分歧。

### 模式 D: Obsidian 样式全景图谱数据导出 (`EXPORT_GRAPH`)
```bash
python3 scripts/wiki_compiler.py export-graph --wiki "/absolute/private/wiki" --output "/absolute/private/wiki/graph_data.json"
```
导出包含 6 大领域分类颜色、节点权重、连接关系的标准 JSON，供前端力导向画布渲染。

---

## 3. 示例与私有输入

`examples/automotive_case/index.md` 只保留历史资料入口。原案例由真实工程资料衍生；替换品牌和名称不使真实阈值、适用范围或符合性答案变成合成数据，因此不随源码分发，也不作为黄金模板。

通用编译器、冲突检测器与检索器继续可用。使用者需提供明确授权的来源与私有输出目录；Wiki 的可检索性不证明产品符合要求。没有适用证据的结论应保留未决，不默认 COMPLIANT。
