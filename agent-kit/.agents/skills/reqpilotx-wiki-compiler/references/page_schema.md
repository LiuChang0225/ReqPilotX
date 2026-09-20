# 原子化 Markdown 词条规范 (Page Schema)

## 1. 结构骨架

```markdown
---
title: 页面名称 (如: iRPS 电子电气硬件、供电拓扑与电路防护规范)
category: specs | ee_hardware | software_diagnostics | dvp_tests | functional_safety | interfaces | integration_rules
tags: [iRPS, Tag1, Tag2]
aliases: [别名1, 别名2]
related_pages:
  - "[[category/page_id_1]]"
  - "[[category/page_id_2]]"
source_evidence:
  - file: "原始文档相对路径或名称.docx"
    section: "§4.1 章节名称"
last_updated: YYYY-MM-DD
---

# 页面名称

## 1. 概念与工程背景 (Overview & Engineering Context)
说明定义、物理机理、设计约束与边界。

## 2. 标准工程参数表 (Baseline Parameters Table)
| 参数项 | 符号 | 最小值 (Min) | 典型值 (Typ) | 最大值 (Max) | 单位 | 条件与工程说明 |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |

## 3. 技术约束与设计准则 (Technical Constraints)
列出不可妥协的硬核工程边界与失效安全行为。

## 4. 关联词条与测试映射 (Related Pages & Verification)
使用 `[[wikilinks]]` 关联上下游规格、试验或集成准则。

## 5. 常见客户偏离与适配指南 (Customer Deviation Guide)
指导需求分析 Agent 如何快速对比外部客户 SOR 条款。

## 6. 原始证据与溯源锚点 (Evidence Anchors)
- `[DOCX]` 文件名 §章节 "原始引文字段"
```
