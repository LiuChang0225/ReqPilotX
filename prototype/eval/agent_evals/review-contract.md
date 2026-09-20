# 评核者的工件解释约定

此文件帮助解释已有工件，不增加生产Agent步骤，也不替代本run冻结rubric。交给评核者的材料应包含原件、case/rubric、具体版本工件和本说明；不只给一份缺少字段语义的JSON。

| 关系或状态 | 应如何核对 |
|---|---|
| 逻辑表所属文档 | `logical_tables[].owner_unit_id`指DOCUMENT/ATTACHMENT单元，正常情况下可以是文档根 |
| 逻辑表直属章节 | 查节点`content_blocks`中的`LOGICAL_TABLE.logical_table_scope_id`；Map编辑输入中的`owner_node_id`会编译成该关系 |
| 原文与标签 | `label/title`不是唯一原文容器。核对source_content/text_bindings、表格单元及source_number，不能只比较标签 |
| 全文保留与分析上下文 | 定义在其它SRC中保留，不代表使用该定义的条目或派生项已经得到关联；应核对实际有效读取包及父级/共享上下文，而非只看字段是否为空 |
| 试验方法与过程类别 | 按项目[分类V1](../../../docs/需求提取/分类与验证关联规则V1.md)，完整试验的条件/方法/判据不因带步骤就自动成为独立PROCESS义务 |
| 程序检查与业务质量 | BYTES_MATCH、REVIEW_VERIFIED、CONTENT_MATCH等只按其真实实现计证据，不自动等于语义、泛化或工程师接受 |

章节归属的代码入口是 `plugins/reqpilot/scripts/artifact_tools.py` 的逻辑表编译及 `source_requirement_baseline.build_source_requirement_scope_request`。不要凭相似字段名推断关系；格式疑问先查当前合同。

明确区分三类发现：实际违反冻结判据、证据不足需REVIEW、评核者提出的后续工程澄清建议。一个合理的新疑问并不自动证明本次提取失败；原文已保留且未补造时，不为追求问题数量扩大失败标准。

同一个关系缺口不自动再计为原文丢失或虚假交付。交接说明的判断需引用终稿真实措辞，按链接/版本/验收声明的证据逐项核实；不能把自己的转述当作模型原文。

对初次误报保留原报告，修订另存。修订时一并纠正其它判据理由中依赖旧误报的交叉表述。独立Agent评核与主Agent裁定分别标注作者，不能改完评核内容后仍冒称是原独立意见。
