# Active Context

更新时间：2026-03-25 16:02 CST  
适用仓库：`/Users/liuchang/Documents/New project`

长期背景：[`/Users/liuchang/Documents/New project/docs/项目.md`](/Users/liuchang/Documents/New%20project/docs/项目.md)

## 当前目标

- 提高 oil pump PDS 在 `main` 上的 extraction/review/export 一致性
- 当前重点：
  - `5.2 / 常见不安全密码算法 / 12 / 15` 这组 residual table-asset 问题收尾
  - `试验大纲` 跨页续表与 merged-cell 起始锚点精度
  - 前端在 `8000` 上复核最新 preview/cache 行为

## 活跃 worktrees

- `codex/extraction-optimization`
  - 目录：`/Users/liuchang/Documents/New project/worktrees/_active/extraction-optimization`
  - 职责：抽取精度与 complex-table V1 落地
- `codex/table-assets-mainline`
  - 目录：`/Users/liuchang/Documents/New project/worktrees/_active/table-assets-mainline`
  - 职责：表格资产主线
- `codex/workspace-stabilization`
  - 目录：`/Users/liuchang/Documents/New project/worktrees/_active/workspace-stabilization`
  - 职责：统一工作台稳定性/性能
- `codex/table-asset-lifecycle`
  - 目录：`/Users/liuchang/Documents/New project/worktrees/_active/table-asset-lifecycle`
  - 职责：表格资产生命周期与瘦身
- `codex/screenshot-precision-maintenance`
  - 目录：`/Users/liuchang/Documents/New project/worktrees/_active/screenshot-precision-maintenance`
  - 职责：后续截图精度/顺序问题专项维护

## 当前样本

- 油泵 PDS：
  - `/Users/liuchang/Documents/New project/prototype/storage/uploads/6fd15d3fb7104ea882d544d10f32372d_产品设计规格说明书PDS-DE前后油泵总成-V1.1-20251215.pdf`
  - 当前 `main` 上这一版 extraction/review/export 结果已作为 benchmark 基线

## 最近已修

- `complex_table_mode.py`
  - complex zone preview 默认改成每个 zone 只保留 `1` 条 exportable 整表 row
  - `17-22` / `23-25` 不再继续生成 summary + group rows 供 review/export
- `table_assets.py`
  - 页段会先做 relevance refine：低相关前后页会被裁掉，numbered section 尾巴可自动延到下一页
  - `5.2 交付物要求` 现在优先选紧凑候选，source snapshot 不再把 `5.3/5.4` 与 `7 附件目录` 一起带进来
  - `常见不安全密码算法` 第一张表已裁回 `26` 页；`Confidentiality Level` / `RASIC` 都固定在 `27` 页
  - `12 温湿度循环(仅防水件)` merged-cell 起始锚点不再把上半部分压掉
  - `15 高温寿命(仅防水件)` 的 continuation page 现在优先用“下一编号前”的专用 bbox，不再把 `16/17` 带进来；page `33` 也不再向上吃 `14`
  - 跨页 numbered section table 不再被误判成 `prefer_clause_window=true`
  - `source-crop` 已升到 `v50`
- `preview_cache.py`
  - preview cache 已升到 `v21`
- 前端审阅页
  - 只要 row 带 `source_snapshot_url`，review/workspace 默认先显示原文图，结构表保留在切换按钮里
  - `查看内容` 与 `导出截图` 文案已拆开，不再把当前显示与 export_variant 混淆
  - 静态资源版本已升到 `20260325-1`
- 真实油泵样本 spot check：
  - `5.2` source snapshot 当前为 `920x250`
  - `常见不安全密码算法` 当前拆成 `26/27/27` 三张 source snapshot
  - `12` 当前 source snapshot 为 `920x859`，上半部分已回到图里
  - `15` 当前 source snapshot 已收敛到 `920x890`，页段仍为 `33-34`，不再带 `14 / 16 / 17`
  - complex rows 当前只剩 `2` 条：`17-22` 一条、`23-25` 一条

## 当前 blockers

- `12 / 15`
  - `12` 已回正；`15` 这轮已进一步去掉 `14 / 16 / 17` 串入，仍需你在前端做最后观感确认
- complex `17-22`
  - 后端已改成单张整表，且 review 默认出图；仍需要确认 fit-width 后在当前页面宽度下是否足够可读
- 前端需要对最新 `preview-cache-v20 + assets 20260325-1` 做一次强刷 + preview refresh，确认没有继续吃旧 JS/旧预览

## 下一步

- 在 `8000` 上人工核：
  - `5.2 交付物要求`
  - `常见不安全密码算法`
  - `12 温湿度循环(仅防水件)`
  - `15 高温寿命(仅防水件)`
  - complex `17-22`
- 如果 `15` 仍觉得还有极窄边界残留，下一步继续细调 numbered continuation page 的 top/bottom 微边距
- 如果 `5.2` 仍被认为“像拆条”，下一步单独收 review 行里 paragraph/table 的显示层级，而不动底层 table markdown 资产
- 如果 complex `17-22` 仍嫌密，下一步做 segmented full-page 视图

## 活跃端口

- `8000`：当前 `main` 本地核查端口
