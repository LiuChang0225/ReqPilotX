# 汽车零部件领域知识本体与自适应文档角色划分指南

## 1. 核心设计哲学：自适应角色分析 (Adaptive Domain Partitioning)

在汽车零部件（Tier-1 / 主机厂）的真实工程场景中，不同供应商或主机厂交付的技术资料命名、目录结构千差万别（例如某些把电气要求写在技术协议，某些把测试与图纸合并在单一 SOR 中）。
**因此，Agent 严禁机械硬性套用固定目录，而必须基于输入文档的实际工程内容与技术特征自适应识别、聚类到以下 6 大领域本体维度**。

---

## 2. 汽车零部件 6 大领域本体维度标准

### 维度 1: 系统规格与物理包线 (`specs` / `system_specs`)
- **工程定义**：零部件在整车或总成系统层级的全局能力包线与边界条件。
- **典型内容**：
  - 机械物理尺寸外包络、额定/峰值工作转速、极限扭矩；
  - 连续/短时工作温区、温升工况谱；
  - 整体工作原理、定转子/传感器拓扑形态（如轴端型 vs 穿轴型）；
  - 专有缩略语与术语表（Glossary）。
- **判定特征词**：`Specification`, `Operating Limits`, `Speed Envelope`, `Working Temperature`, `Envelope`, `Principle`.

### 维度 2: 电子电气硬件与电路接口 (`ee_hardware`)
- **工程定义**：零部件板级硬件、供电拓扑、电路防护、信号驱动与物理接口规范。
- **典型内容**：
  - 供电电压包线（如 5V 精密稳压 vs 12V 蓄电池直接供电）、静态/动态功耗；
  - 过压钳位、反接保护电路（TVS、防反二极管/PMOS 拓扑）；
  - 模拟信号前端（AFE）、正余弦差分/单端驱动电路、共模电感滤波；
  - EMC/ESD 硬件防护网络（ISO 10605 静电放电、ISO 7637-2 瞬态脉冲吸收）；
  - Pinout 针脚定义、接插件型号（如 USCAR、FCI-Minitek）及导线材质（如 FLR13Y）。
- **判定特征词**：`E/E`, `Hardware`, `Schematic`, `Power Supply`, `LDO`, `Pinout`, `Connector`, `ESD`, `TVS`, `Choke`, `Overvoltage`.

### 维度 3: 嵌入式软件与车载诊断 (`software_diagnostics`)
- **工程定义**：专用 ASIC / MCU 内部运行的嵌入式解算逻辑、通信协议与故障诊断机制。
- **典型内容**：
  - 角度解调与跟踪环路（Angle Tracking Observer ATO / PLL 带宽与动态滞后）；
  - 信号质量实时自标定（幅值失真、正交度自校正、零点漂移补偿）；
  - ISO 26262 硬件级故障监控状态机（Loss of Signal LOS、Loss of Tracking LOT）；
  - 安全故障导向电平（High-Z 三态高阻、安全拉低/拉高）；
  - 车载数字通信协议（SAE J2716 SENT 协议、PWM 输出、SPI、CAN FD）；
  - UDS (ISO 14229) 车载诊断协议故障码（DTC）映射与 AUTOSAR CDD/MCAL 适配。
- **判定特征词**：`Software`, `Firmware`, `Algorithm`, `Tracking Loop`, `ATO`, `PLL`, `Diagnostics`, `DTC`, `UDS`, `SENT`, `AUTOSAR`, `State Machine`.

### 维度 4: 试验规程与工况验证 (`dvp_tests`)
- **工程定义**：设计验证与生产确认计划（DVP&R）中的标准试验用例与严苛工况。
- **典型内容**：
  - 环境耐久：高低温循环、热冲击、耐高温润滑油/ATF 自动变速箱油 1000h 浸泡；
  - 机械物理：三轴宽频随机振动（ISO 19453-3）、机械冲击（50g/100g）、自由跌落；
  - 电磁兼容（EMC）：大电流注入（BCI）、辐射发射/抗扰、高强直流杂散磁场抗扰（>4000 A/m）；
  - 极限几何：装配气隙极限公差扫描、多轴倾斜偏角动态运转验证。
- **判定特征词**：`DVP&R`, `Test Spec`, `Validation`, `Thermal Shock`, `Vibration Profile`, `Fluid Compatibility`, `EMC Test`, `BCI`.

### 维度 5: 功能安全与可靠性 (`functional_safety`)
- **工程定义**：符合 ISO 26262 车规功能安全标准的系统级安全目标、危险与风险分析。
- **典型内容**：
  - ASIL 安全等级目标（ASIL-B / ASIL-C / ASIL-D）；
  - 安全机制（Safety Mechanism SM）与故障注入测试用例；
  - 单点故障度量（SPFM）与潜在故障度量（LFM）；
  - DFMEA / PFMEA 失效模式与失效严重度分析。
- **判定特征词**：`ISO 26262`, `ASIL`, `Safety Goal`, `FMEDA`, `Safety Mechanism`, `Fault Injection`, `DFMEA`.

### 维度 6: 系统集成与偏离准则 (`integration_rules`)
- **工程定义**：零部件与主机厂整车总成集成时的机械配合公差链、匹配建议与客户 SOR 偏离处置。
- **典型内容**：
  - 气隙与同轴度公差链计算指南（均方根法 RSS 叠加）；
  - 紧固螺栓推荐力矩与配合公差（H7/g6）；
  - 常见客户 SOR 超差条款清单与技术偏离（Deviation）评估标准与流转 SOP。
- **判定特征词**：`Tolerance Stackup`, `RSS`, `Integration Guide`, `Deviation SOP`, `Mounting Rules`, `PCD`.

---

## 3. 自适应文档分类决策流程 (Decision Heuristics)

当处理一个新技术文档（PDF/DOCX/Drawing/Excel）时，按如下步骤判定：
1. **优先识别内容实质而非文件名**：即使文档名为 `Specification.pdf`，如果其 80% 内容为电路原理图与 Pinout，应归入 `ee_hardware`；如果主要为振动曲线与试验条件，归入 `dvp_tests`。
2. **多主题综合文档的原子化拆分**：长篇综合技术协议不得整篇合为一个大文件，而应提炼为对应各个维度的原子页面，并通过 `[[wikilinks]]` 互相链接。
3. **严格源证锚定**：每个提取的参数必须在 `source_evidence` 中标明原始文档的文件名、章节编号及物理页码。
