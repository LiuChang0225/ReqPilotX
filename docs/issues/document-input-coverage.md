+++
id = "document-input-coverage"
title = "新文档全文目标态与跨格式泛化"
status = "open"
severity = "high"
category = "document-support"
summary = "已有来源与局部 Agent/UI 资产；新文档完整条目、分类、派生拆分及跨格式泛化仍需用户案例验收。"
updated_at = "2026-09-03"
+++
# 新文档全文目标态与跨格式泛化

## 当前事实

PDS 已有同树条目、分类与反馈资产；F06 全文来源与两章局部结构已进入工作台。
旧“native SourceIR 只能 shadow capture、只能逐格式切 serving”的陈述不再是当前路线。
来源适配器、规则基线、视觉和旧 corpus/oracle 仍作为工具与回归资产保留。

## 当前缺口

新文档全文的原文归属、条目边界、分类、派生拆分、前端核对与用户反馈尚未整体完成。
PDF/DOCX/XLSX 与扫描/视觉件的泛化不能由一个局部案例或历史断言数量推定。

## 验收方式

按[开发计划](../需求提取/开发计划.md)先完成真实案例目标态，由产品负责人核对；
再做新会话、新文档和目标宿主的复现。SourceIR COMPLETE、自动覆盖检查、用户接受与
生产发布分别记录。当前具体缺口见[差距矩阵](../需求提取/当前实现差距矩阵.md)。

旧测试计数、adapter 版本与 formal shadow 步骤只在历史记录中查询，不作为新阶段门禁。
