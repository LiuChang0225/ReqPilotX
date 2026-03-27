# Extraction Active Context

更新时间：2026-03-27 17:03 CST  
分支：`main`

## 分支目的

- 用 oil pump PDS 维持 benchmark 基线
- 用 减振器 PDS 回归试验大纲截图精度
- 章节顺序已经稳定，当前只看 source snapshot / table asset 精度
- 当前以 `8000` 上的真实 review/workspace 结果为准
- `U:96 / REQ-0231` 与油泵 PDS 第 30 页缺失都已收口，当前转入观察残余回归与清理测试抖动

## 当前样本

- benchmark：`/Users/liuchang/Documents/New project/prototype/storage/uploads/6fd15d3fb7104ea882d544d10f32372d_产品设计规格说明书PDS-DE前后油泵总成-V1.1-20251215.pdf`
- 回归：`/Users/liuchang/Documents/New project/prototype/storage/uploads/1e4079e33aef4432bb379d5d0ef95d50_产品设计规格说明书PDS-DE全主动减振器总成-V1.0-20251127.pdf`

## 最近已修

- `prototype/app/services/table_assets.py`
  - 单行条目中，`DV/PV` / supplier / sample 这类行内单元格不再被误当作 `end_anchor`
  - generic start-anchor（如 `项目`）不再把 `W` 类小信息表裁成空白
  - compact labeled info table 不再从 candidate-window 误退回 clause-window
  - heading anchor context 现在保留编号；plain-numbered heading 不再被拆成宽泛中文 fragment
  - 新增“section_end_anchor 仅在脏单行场景继续参与收紧；干净单行且锚点紧贴下一编号行时忽略该锚点”的保护，修复 `U:96`
  - `source snapshot` 签名版本已提升到 `source-crop-v57`，用于强制失效旧截图缓存
- `prototype/app/services/preview_cache.py`
  - preview cache 当前版本为 `preview-cache-v28-numbered-heading-anchor-fragments`
  - preview cache 现在同时比较 `_preview_build_token`，crop 签名变化会自动判 stale
- `prototype/app/static/app.js`
  - 默认 table variant 现在尊重后端 `export_variant`
- `prototype/app/static/extraction-review.js`
  - review 页同步尊重后端 `export_variant`
- `prototype/tests/test_table_assets.py`
  - 新增 `W`、`U:46`、`U:94` 以及 `U:96/63` 这类问题对应的回归断言

## 当前样本状态

- oil pump：
  - benchmark 继续作为章节 / export 基线
  - 此前出现的 PDS 第 30 页缺失已确认解决
- damper：
  - 两个文档副本 `7d914049...` / `ec8c4168...` 都已重建到 `preview-cache-v28-numbered-heading-anchor-fragments`
  - `W 整车应用基本信息` 当前 source snapshot 不再空白
  - `U:46` 当前 source snapshot 约 `56px` 高
  - `U:94` 当前 source snapshot 约 `58px` 高
  - `U:57/58/59/60` 已确认根因是“新 crop plan 正确，但旧 PNG 在未换签名时被复用”；当前 4 张 source snapshot 已重建为不同文件
  - `U:96 / REQ-0231` 已确认根因是“下一编号行 `63 EMC` 被当作当前单行条目的结束锚点”；真实刷新后同一路径 `src_61100411333c474a.png` 高度已从 `44px` 回升到 `83px`
  - 用户已确认 `U:96` 页面效果正常
  - 这轮已确认顺序问题不再是主线

## 当前已知问题

- 当前无已知截图主线 blocker；若再报新编号，再按 anchor/crop plan 链路复现
- `tests.test_unified_workspace` 当前存在 SQLite tempdir `-wal/-shm` 清理抖动，失败点在 `tearDown()`，不是本轮 table-assets 断言失败
- 试验大纲/附件起点的人机确认锚点仍是第二期，不在本轮代码内

## 下一步

- 优先清理 `tests.test_unified_workspace` 的 SQLite tempdir `-wal/-shm` 清理抖动
- 若还有残余问题，先打印：
  - `before_context / after_context`
  - `start_anchor / end_anchor`
  - `row_slice / crop plan`
- 版本/缓存策略默认继续走：
  - 每文档单套 live preview assets
  - 算法版本变化通过 build token 自动失效，不保留大量历史 preview PNG
- 若 `8000` 不可达，优先执行：
  - `cd prototype && ./status.sh`
  - `cd prototype && ./start.sh`
