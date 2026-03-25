# Extraction Active Context

更新时间：2026-03-25 16:02 CST  
分支：`main`

## 分支目的

- 继续维护 oil pump PDS 的 extraction / table-asset 精度
- 当前主任务：
  - `软件开发要求` 和 `试验大纲` 的截图边界精度
  - complex table 默认改成单张整表
  - `8000` 上做真实前端复核

## 当前样本

- `/Users/liuchang/Documents/New project/prototype/storage/uploads/6fd15d3fb7104ea882d544d10f32372d_产品设计规格说明书PDS-DE前后油泵总成-V1.1-20251215.pdf`
- 当前 `main` 上这一版 extraction/review/export 结果已作为 benchmark 基线
- 重点页段：
  - `17-22` 网络安全/数据安全矩阵
  - `23-25` 生命周期矩阵
  - `12` 特殊特性
  - `29-34` 测试分组 / 试验大纲

## 最近已修

- `complex_table_mode.py`
  - complex zone preview 改成每个 zone 只保留 `1` 条整表 row
  - `complex_role` 仍沿用 metadata，但默认不再落 summary/group 多条 synthetic rows
- `table_assets.py`
  - `5.2` 优先选紧凑候选，截图不再把 `5.3/5.4` 与 `7 附件目录` 一起带进来
  - `常见不安全密码算法` 首表固定在 `26` 页，`Confidentiality Level / RASIC` 固定在 `27` 页
  - `12` 的 merged-cell 起始锚点已放宽，上半部分重新进入 source snapshot
  - `15` 的 continuation page 现在优先取 `16` 前的 continuation 段，不再把 `16/17` 裁进去；page `33` 顶部也不再向上带 `14`
  - 跨页 numbered section table 不再被误判到 clause-window 分支
  - `source-crop` 已升到 `v50`
- `preview_cache.py`
  - cache 已升到 `preview-cache-v21`
- 前端审阅页
  - 默认查看模式改成：只要有 `source_snapshot_url` 就先显示原文图
  - `查看内容` 与 `导出截图` 已拆开，减少“显示=导出”的误解
  - 静态资源版本已升到 `20260325-1`

## 当前样本核查

- `5.2`：当前 source snapshot 为单页紧凑图；review 默认先显示原文图
- `常见不安全密码算法`：不再把 page `27` 整页误并进首表
- `12`：当前 source snapshot 已包含此前缺失的上半段
- `15`：当前页段仍为 `33-34`，`slice_rows` 为 `13` 行，source snapshot 已收敛到 `920x890`
- complex rows：当前只剩 `17-22` / `23-25` 两条整表 row

## 当前已知问题

- `15` 已去掉 `14 / 16 / 17` 串入，仍需真实页面最后确认体感是否足够干净
- complex `17-22` 的 fit-width 可读性仍需真实页面确认
- 前端需要基于 `preview-cache-v21 + assets 20260325-1` 强刷并重建 preview，确认没有继续复用旧图或旧 JS

## 下一步

- 在 `8000` 上人工核：
  - `5.2 交付物要求`
  - `常见不安全密码算法`
  - `12 温湿度循环(仅防水件)`
  - `15 高温寿命(仅防水件)`
  - complex `17-22`
- 如 `15` 仍觉得还有极窄边界残留，继续细调 numbered continuation page 的 top/bottom 微边距
- 如 complex `17-22` 仍嫌密，做 segmented full-page 视图
