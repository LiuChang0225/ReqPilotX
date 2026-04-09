# Extraction Active Context

更新时间：2026-04-09 23:23 CST  
分支：`codex/extraction-quality-loop`

## 分支目的

- 专注拆解质量与批量回归闭环
- 扩展样本集、问题分类法与 benchmark-safe 优化
- 不承担 workspace、auth、RAG 主线治理任务

## 当前样本

- 当前关注样本：批量投喂目录与既有 benchmark（尤其油泵 PDS）
- 当前主验证端口：`8000`
- 当前关注链路：parser / extractor / extraction preview / batch regression

## 最近已修

- 历史 extraction active-context 现已收窄为拆解专项上下文，不再作为所有窗口的默认必读
- 控制塔、track registry、track skill、trackctl 已把拆解优化收口为独立 `extraction-quality-loop` 轨道

## 当前已知问题

- 对应 worktree 还没创建，控制塔当前会把这条 active track 保持在 `TODO / ON_PLAN / not_started`
- 批量样本目录、统一问题分类法和自动报告还没有正式收口
- 需要确保新样本优化不会击穿既有 benchmark

## 当前验证结论

- extraction 支线已经从全局上下文中解耦，后续只由对应 worker 读取和维护

## 下一步

- 创建 `codex/extraction-quality-loop` worktree
- 开工即运行 `python prototype/scripts/trackctl.py start --track extraction-quality-loop`
- 建立批量运行与报告入口
- 明确 benchmark 门禁与问题分类法
- 在控制塔内以独立轨道持续推进
