+++
id = "repository-architecture-structure"
title = "现有应用分层与后续能力边界"
status = "resolved"
severity = "high"
category = "architecture"
summary = "旧 main.py/app.js 集中与双前端问题已由薄入口、分域用例和 React 工程收口；现有框架不限制目标态探索。"
updated_at = "2026-09-03"
+++
# 现有应用分层与后续能力边界

## 已解决的原问题

现有 app/main.py 是薄入口，HTTP routers、application、domain、ports、adapters 与 worker 已分开。
浏览器源码在 prototype/frontend，旧 app/static/app.js 不再是运行入口。
因此早期“必须继续从 main.py/app.js 迁出”及 strangler 计划不再是当前开放任务。

## 保留边界

来源、业务对象、宿主运行与 UI 投影不能混作同一权威。
存在大型 adapter 或用例文件，不等于应该无目标地重构整仓；
实际影响目标态或维护安全时，再根据调用关系做聚焦调整。

第一阶段可以隔离替换框架；阶段二收敛能力；阶段三加载同一核心。
当前设计见[应用架构](../architecture/target-architecture.md)。

本条 resolved 仅指原来的入口/分层问题，不声称整个 Agent 工作流、所有大文件或发布已完成。
