# RAG Quality Active Context

更新时间：2026-04-09 23:23 CST  
分支：`codex/rag-quality-loop`

## 分支目的

- 建立 requirement -> evidence 的标注与评测闭环
- 在不破坏现有 Azure 基线的前提下调优召回与判断质量

## 当前关注

- control tower / trackctl / skill 已就位，等待该支线真正开工
- `sample10` 基线
- labeled evidence set
- evidence hit / wrong evidence / missing evidence 指标

## 当前 blocker

- 对应 worktree 还没创建，控制塔当前会把这条 active track 保持在 `TODO / ON_PLAN / not_started`
- 真实标注集与统一报告还没有收口

## 下一步

- 创建 `codex/rag-quality-loop` worktree
- 开工即运行 `python prototype/scripts/trackctl.py start --track rag-quality-loop`
- 先把控制塔、track skill、checkpoint/gate 流程接通，再开始 RAG 数据闭环
