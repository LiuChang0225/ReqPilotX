# Active Context

更新时间：2026-03-27 17:03 CST  
适用仓库：`/Users/liuchang/Documents/New project`

长期背景：[`/Users/liuchang/Documents/New project/docs/项目.md`](/Users/liuchang/Documents/New%20project/docs/项目.md)

## 当前目标

- 在 `main + 8000` 上维持 oil pump benchmark 与 damper 回归样本的截图稳定性
- `U:96` 与油泵 PDS 第 30 页缺失问题都已确认解决，主线从“修当前问题”转为“观察是否还有残余误截”
- 当前优先级转向清理非主线抖动，并保留现有缓存/版本失效策略

## 当前样本

- benchmark：`/Users/liuchang/Documents/New project/prototype/storage/uploads/6fd15d3fb7104ea882d544d10f32372d_产品设计规格说明书PDS-DE前后油泵总成-V1.1-20251215.pdf`
- 回归样本：`/Users/liuchang/Documents/New project/prototype/storage/uploads/1e4079e33aef4432bb379d5d0ef95d50_产品设计规格说明书PDS-DE全主动减振器总成-V1.0-20251127.pdf`

## 最近已修

- `prototype/app/services/table_assets.py`
  - 修掉单行条目里 `DV/PV` 等行内单元格误判成 `end_anchor`，避免 `T:4 / T:5 / U:37 / V:1` 这类截图把整页尾巴带进来
  - 增加“晚到的泛化起点锚点”保护，`项目` 这类 generic cell 不再把 `W 整车应用基本信息` 裁成空白
  - 收紧 source retry：两列表/小信息表不再从正确 candidate-window 回退成错误 clause-window
  - 收紧 numbered heading 的 anchor fragments，`46/47/94/95` 这类 heading 不再拆成宽泛中文片段，避免命中更早的相似行
  - heading 上下文现在保留 section code，避免 `46`、`94` 之类试验条目丢编号后模糊匹配
  - 新增“干净单行条目忽略紧贴下一编号锚点”的保护，避免 `U:96 防尘罩拔脱` 这类条目被下一行 `63 EMC` 压成超薄截图
  - `source snapshot` 签名版本已提升到 `source-crop-v57`，配合 preview build token 自动淘汰旧 PNG
- `prototype/app/static/app.js`
  - 修正 table view 默认变体判断：当后端明确给出 `export_variant=slice` 时，前端不再无条件强切到 `source`
- `prototype/app/static/extraction-review.js`
  - 同步修正 review 页 table variant 逻辑
- `prototype/app/services/preview_cache.py`
  - preview cache 当前版本提升到 `preview-cache-v28-numbered-heading-anchor-fragments`
  - cache 现在额外校验 `_preview_build_token`，source-crop 版本变化会把旧 preview 直接判 stale
- `prototype/tests/test_table_assets.py`
  - 新增回归覆盖：inline cell end-anchor、late generic start-anchor、compact info table retry、heading anchor context、plain-numbered heading fragments、`U:96/63` 单行误裁场景
- 真文档回放：
  - damper 两个文档副本 `7d914049...` / `ec8c4168...` 都已重建到 `preview-cache-v28-numbered-heading-anchor-fragments`
  - 主文档关键截图当前已验证：
    - `W 整车应用基本信息` 不再空白
    - `U:46` 当前 source snapshot 高度约 `56px`
    - `U:94` 当前 source snapshot 高度约 `58px`
    - `U:57/58/59/60` 当前 source snapshot 已重新生成并拆分成 4 张不同图片，不再复用同一张旧缓存
    - `U:96 / REQ-0231` 当前 source snapshot 路径仍为 `src_61100411333c474a.png`，高度已从 `44px` 回升到 `83px`
    - 用户已确认 `U:96` 页面观感正常，问题收口
  - oil pump benchmark：
    - 此前出现的 PDS 第 30 页缺失已确认解决

## 当前 blockers

- 当前无已知 `table_assets / source snapshot` 主线 blocker
- `tests.test_unified_workspace` 当前有一个 SQLite tempdir `-wal/-shm` 清理抖动：
  - 失败点在 `tearDown()` 删除临时目录，不是本轮 table-assets 逻辑断言失败

## 下一步

- 若再出现新的误截或缺页，优先看三类根因：
  - generic start/end anchor 仍误命中
  - clause-window retry 仍覆盖 candidate-window
  - preview 页面仍持有旧 preview 数据或旧 asset URL
- 优先清理 `tests.test_unified_workspace` 的 SQLite tempdir `-wal/-shm` 清理抖动
- 后续版本/缓存策略默认沿用：
  - 每文档只保留一套 live preview assets
  - 结构/裁切算法升级靠 build token 自动失效，不长期堆历史 PNG
- 若用户继续报具体编号，先用当前 `v28` 链路复现并打印 `start_anchor / end_anchor / row_slice / crop plan`
- 日常使用统一走：
  - `cd prototype && ./start.sh`
  - `cd prototype && ./status.sh`
  - `cd prototype && ./stop.sh`
  - 调试时用 `cd prototype && ./start-dev.sh`

## 活跃端口

- `8000`：当前 `main` 稳定 review/workspace 端口（`./start.sh` + `screen`），`./status.sh` 当前 `HTTP: 200 OK`
