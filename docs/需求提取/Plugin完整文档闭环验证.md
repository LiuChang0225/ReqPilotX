# Plugin 完整文档闭环验证

更新：2026-09-11。接续[完整性与原序首轮](Plugin完整性与原序首轮.md)，按用户要求只推进 plugin；模型固定 Sol high、Luna max。App 专项入口及 UI 扩展后置。

## 结论与边界

Sol high 在一份新建五页合成 SOR 上完成了原件读取、Document Map、SRC/REQ/分类、授权拆分、图片释读和程序 Excel。第二轮为 17 SRC、13 个原文 REQ、2 个 Snn、1 张原图，原序、编号、跨页正文、BOM 和完整试验均有核对证据。但模型声明的 SKIPPED 分析策略没有进入最终版本。随后在独立副本中用修复后的程序导入策略、导出并完成 review-close/review-verify，保留原模型成绩。

Luna max 在相同 v2 包、原件与任务下，30 分钟内产出 Map 和 17 SRC/12 REQ，未完成标注、拆分和 Excel。不能认定已达到可用的完整流程，也不能仅据这一轮判定模型能力上限。

本轮是**完整合成文档的开发验证**。不是客户长文档泛化测试，没有宣称人工接受，没有启动独立模型验收 Agent。前轮真实文档限定范围验证继续保留，不与本轮拼成整本客户文档通过。

## 冻结输入与实际环境

- 私有根：`prototype/runtime-data/plugin-full-chain-20260911/`，已忽略，不进入插件。
- 原件：`source/synthetic-actuator-sor.pdf`，SHA256 `01388279403d8ce789695b91679bcd2e5935293099338cc53f6bc93e7d2682f9`。
- 五页覆盖：整块封面/目录；有编号的正文；物理原序与 PDF 绘制顺序不一致；跨页句子；带共享条件的需求；BOM 合并单元格；跨页 V-01 与独立 V-02；表标题/续表标题；接线原图；交付、过程、参考说明。
- 根 Agent 实际检查了五页渲染。`evaluator/oracle.json` 在首次运行前冻结，未提供给生产模型；`verify_result.py` 在评测目录中独立执行。
- 三次都用真实 Codex CLI 新会话、完整主 skill 及相应职责 skill。隔离工作区只提供原件、任务和插件，禁用环境项目规则和多 Agent；未灌入旧 Map、旧答案或评测 oracle。
- 任务明确：无内部知识库，跳过符合性分析；已授权完整离线草稿，各阶段保存后可自主继续。这是评测授权，不代表修改日常用户反馈/确认约定。
- Sol v1 包 SHA `c0c1fb0ebb391415aa2fcca82febe135bec3ecb665fd98a98599575519ef696b`。Sol/Luna v2 使用同一冻结包，实际构建回执 SHA `3615f1af6a466b312ad5f9801932808356d04ff48d91a5d982442cdce0fd3ff4`。三轮运行结束检查均为 UNCHANGED。
- v2 两模型同期运行，单次样本且可能受资源/服务波动影响；耗时是观察结果，不作严格性能因果结论。

## 完整保留的成绩

| 实际运行 | 首 Map | 首 SRC 基线 | 首 Excel | 总耗时 | 完成命令 / 失败 | 结果 |
|---|---:|---:|---:|---:|---:|---|
| Sol high v1 | 252 秒 | 508 秒 | 859 秒 | 1051 秒 | 63 / 4 | 19 SRC、13 REQ、6 Snn；两个纯表标题被建成非需求 SRC |
| Sol high v2 | 218 秒 | 418 秒 | 773 秒 | 997 秒 | 56 / 6 | 17 SRC、13 REQ、2 Snn；纯表标题修复，SKIPPED 策略未进入版本 |
| Luna max v2 | 705 秒 | 1715 秒 | 未产出 | 1810 秒含中断收口 | 101 / 10 | 17 SRC、12 REQ；达到 1800 秒限制，完整链路未完成 |

首件时间来自文件创建时间相对实际事件文件的观察，近似表示阶段落地时间。失败命令包含正常修正中的 shell/参数/校验错误，不等于相同数量的语义错误。实际看图返回分别为 5、6、5 个图片块；CLI command-item 事件可能遗漏 exec 中的看图，另从对应会话的公开工具调用/响应核对，不保存隐藏推理。

Sol v1/v2 的最终 usage 分别为：input 10,740,313 / 8,743,089，cached input 10,513,792 / 8,530,304，output 43,661 / 36,067。reasoning output 已包含在 output 中，不重复相加。Luna 无 turn.completed usage，不能写成零或虚构总成本。实际回执与公开调用位于各运行的 `workspace/runtime-data/cold-session/`，摘要在各自 `source-assessment.json`。

首次 v1 摘要曾只统计 2.5 的两个 Snn，完整交付复核纠正为 6 个：另有 V-01 两个验收目标与 5.2 两个过程动作。它们的原文父项仍完整保留。修正摘要单独保存为 `sol-high-v1/source-assessment-v2.json`；原始摘要不覆盖。冻结 oracle 对 2.5 有明确拆分要求，没有将全篇 Snn 总数固定为 2；不同模型结果的额外拆分粒度仍应单独核对，不能把条目数当语义验收。

## 发现、责任层与已落地修复

### 1. 表标题误建 SRC：先修来源角色

Sol v1 把纯表标题放入 Map 正文。条目阶段试图将它作为 structure-only，被 `SOURCE_ITEM_STRUCTURE_ONLY_SPAN_NOT_TITLE` 拒绝；模型为通过校验改成非需求 SRC。这是 Map 角色错误与修复路径不清叠加，不是应放宽正文覆盖规则。

现在该拒绝带 scope、node、content ID，指出回查原件并局部修 Map。纯表标题保留在逻辑表标题及精确 evidence_refs，配明确来源处置；有实际条件/说明/义务的标题内容仍须保留为有效正文或表格内容。不能伪造一个章节或非需求 SRC 绕过校验。

工具正反例已进入 `tools-map-edit.md`；source-map/source-items skill 指向同一修复方法。隔离程序探针修复后为 17 SRC，原件/表格/旧 Map 不变；随后 Sol v2 自主运行也未再产生两个纯标题 SRC。不是事后只保留成功输出。

### 2. 分析策略只有文件、没有交接入口

Sol v2 已写 `run/analysis-policy.json`，但 review-open/review-revise/deliver 没有暴露对应参数；文件存在并不意味着当前版本接纳它。

已增加 `--analysis-policy`，校验来源与可选基线，保存到新版本；返回明确的 policy 状态。兼容本次实际出现的 `reason_code` 加说明性 `reason`，转成规范原因码与 note，冲突表达仍拒绝。兼容只用于明确导入，历史保存状态及其哈希不重写，不自动猜测用户是否跳过分析。

### 3. 导出仍依赖 Agent 重复拼接已保存状态

政策修复的程序探针又发现：`deliver --baseline-dir <当前版本>` 自动读取图片标注，却不自动读取同目录已保存的 decomposition/assessments。遗漏参数时 Excel 缺 Snn；review-close 正确拒绝了该交付。

现在程序自动继承该版本的标注、拆分、分析及其精确 packet、人工意见和分析策略，保留显式替换入口及来源验证。缺少分析必要依据时拒绝，不静默丢弃分析。普通导出无需 Agent 重新找文件、拼 JSON 或再全文阅读 Excel。

这回答了此前“Excel 错是否证明前边映射错”：**不一定**。本例 Map 和拆分工件正确，缺项发生在导出输入组装。应依次核对当前版本工件 → 实际 Matrix snapshot → 实际 XLSX；不能只凭文件存在或总数归因。

### 4. 原因码排序与标注工具发现

Luna 一次因为 reason_codes 未排序而重试。排序/去重属于确定性格式工作，现由 compile-items 适配器完成；非法原因码仍拒绝，原文、源序、分类及边界不变。

Luna 后期多次检索内部标注实现。`compile-annotations` 本已存在，但条目 skill 的后续入口不够直接。现从 source-items 与 tools-items 指向其公开输入合同，并说明跨 scope 的上级条件使用已有 SRC 的 context_source_ids，不能塞入外来 shared_source_content_id。这项指导是否改善模型行为仍待复测，不能用文档变更冒充效果。

### 5. 评测也需追到真实交付

最初评测脚本仅检查拆分工件存在和内容正确，没有检查拆分已进入 snapshot/Excel。这使第一次程序修复探针在漏 Snn 时仍通过该项；后续版本闭环校验发现问题。已追加实际子项身份、Excel REQ-Snn 行、父后顺序及全文核对，失败探针与旧评测保留。

修复后的程序副本在 `evaluator/policy-handoff/review/deliveries/policy-candidate-v2/`。`verification.json` 证明原模型工作区未改，Map、SRC 基线、表格、标注和拆分字节未改，只增加政策版本；review-close/review-verify 通过。`program-verification-v2.json` 检查实际交付中的 17 SRC、13 REQ、2 Snn、原图像素、同版快照和 SKIPPED，无失败项。这是程序修复成绩，未替换 Sol v2 的模型成绩。

## Luna 归因与下一轮设计

十个失败命令可定位为：四次 if/fi 命令拼接错误，两次 jq 对 null 迭代，两次串联源码搜索未命中，一次原因码排序，一次共享内容引用错误。仅这些错误不能解释全部耗时；日志也显示持续读取合同/源码、检查 ID 与候选字段。当前证据支持“工具交接仍有负担”，不足以分离模型速度、推理策略和框架影响。

最终 17 SRC 保留 13 个原编号条款、整块封面目录、BOM 与跨页试验，没有两个纯表标题 SRC。12/13 REQ 的差异来自共同运行条件被 Luna 作为非需求上下文保留，不能仅按总数判错；最终适用链接、图片释读、拆分与交付仍未完成，不能据局部内容正确宣布整套通过。

下一增量：冻结“已有 Map/SRC → 编号与上下文标注 → 拆分 → 无 KB 策略 → 当前版本导出”范围，用材料齐备的 Sol high/Luna max 对照定位交接成本；它单独计为阶段复现，不冒充原件冷启动。先审计编制命令是否能生成最小可填写输入与定位错误，再决定补公共模板或工具，避免让 Agent 手抄证明、ID、原文和坐标。该范围稳定后再用未调参的新文档独立全链路验证。App 与 UI 按唯一计划后置。

## 发布与验证

- 63 项插件测试、10 项标题修复/Map 编辑/来源读取回归通过；两份修改的职责 skill 校验、插件结构校验、冻结包 doctor READY。
- 新个人插件 `0.3.0+codex.20260911143921` 已 installed/enabled，构建/个人源/缓存 147 文件一致；包 SHA `532068c4f511036001e54ce16d36c088dec142e13bb6cb1a04b42168fc28989f`。
- 安装后使用实际个人插件重新执行 review-verify，通过。回执在 `final/installation-verification.json`、`final/installed-review-verification.json`、`final/enabled-state.txt`；旧个人源保留可回退。
- 新任务才能读取新版技能。最后几项程序修复后未再启动付费模型；Luna 行为效果与新文档泛化未验收。
- 未重载主 App 服务，未改客户正式基线，未推送仓库。
