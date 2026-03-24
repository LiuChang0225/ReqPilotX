# Extraction Active Context

更新时间：2026-03-24 14:17 CST  
分支：`codex/extraction-optimization`

## 分支目的

- 落地 oil pump PDS 的 extraction 精度修复
- 当前主任务：`Complex-Table Mode V1` + 普通 source snapshot 精度恢复 + `3.2.2` 这类 split-clause 顺序稳定

## 当前样本

- `/Users/liuchang/Documents/New project/worktrees/_active/extraction-optimization/prototype/storage/uploads/9b0b3383c2b24ac09ac34bf24974b01e_产品设计规格说明书PDS-DE前后油泵总成-V1.1-20251215.pdf`
- 重点页段：
  - `17-22` 网络安全/数据安全矩阵
  - `23-25` 生命周期矩阵
  - `13` 作为安全回归页

## 最近已修

- 新增 `prototype/app/services/complex_table_mode.py`
  - 定义 complex zone/group 数据结构
  - 落地样本优先规则：
    - `merged_requirement_matrix`
    - `lifecycle_matrix`
    - `generic_complex_table` 仅 metadata，不 collapse；当前对油泵 sample 默认关闭
- `extraction_preview.py`
  - 普通 `_build_section_unified_entries` 先跑，再 rewrite `complete_requirement_entries` 和 `complete_requirement_entries_raw`
  - synthetic complex rows 不再参与通用 dedupe
  - zone collapse 改成 selective rewrite，不再按整页粗暴吞并
- `table_assets.py`
  - complex row 走专用 enrich 分支
  - summary/group 都生成 source snapshot
  - complex ref 强制 `source + locked`
  - 已修复“已有 source snapshot 但不持久化 asset”，保存后 `table-assets` API 可正常读取
  - 修复了 row-slice bbox、source snapshot 尺寸和单行试验大纲表头回归
  - 新确认并修复截图回归：
    - 普通 source snapshot 不再被全局高度上限压缩
    - full-page span 总览长图仍保留高度上限
    - `source-crop` 签名已 bump，避免继续复用旧 PNG
  - 本轮继续修普通截图：
    - 结构化大表不再默认误走 `clause-window`
    - `anchor-window` 的 text/image/drawing bbox 会先裁到窗口内，防止 `8.2.5.3` 吃进 `8.1.3`
    - 单行表格在遇到下一节 heading 时允许更早收口，`3.1` 不再带出 `3.2 / 3.2.1`
- `preview_cache.py`
  - preview cache 新增版本键
  - 旧 preview JSON 会自动判 stale，避免前端继续读到坏截图
  - 当前 cache 版本已升到 `preview-cache-v4`，用于淘汰修复前仍会把 `3.2.2` 子条目排错的旧 preview
- `extractor.py`
  - top-level short clause with terminal punctuation 现在也会被识别成独立 local clause
  - `REQ-0069`（`10）表面处理要求以DVP为准。`）已恢复为 page `8` 的独立条目，不再乱入 `3.2.2`
- `extraction_preview.py`
  - synthetic split-clause section 不再因为跨页 requirement 的后续行号被重新对齐
  - 已修复 `3.2.2` 中 `4）/6.4）` 抢到 `2）/3）`、`5）/6）` 前面的错序问题
  - 新生成 preview 中，`3.2.2` 顺序已恢复到 `1）→2）→3）→4）→5）→6）→6.1）→6.2）→6.3）→6.4）...`
- `app/static/index.html` / `app/static/app.js` / `app/static/styles.css`
  - matrix 面板新增草稿提醒 banner
  - 条件：条目集已生成但尚未锁定
  - 文案：`未锁定条目集，非正式 Compliance Matrix。锁定后再作为正式版本导出。`
- `main.py` / `db.py`
  - complex metadata 经 review/save/apply/workspace/export 不丢失
  - complex row 保存时跳过破坏性重算
  - workspace fallback 改成按 `complex_shadow_signatures` 精确屏蔽被吞掉的旧 preview rows
  - req-code 相关身份匹配已收紧：
    - workspace 排序不再把 stale `req_code` 当成 preview 顺序主键
    - preview overlay / review-save / preview-context enrich / table-assets materialize 统一改成 source-signature 优先
    - `req_code` 只在缺乏 source context 时做弱兜底，避免 preview 重建后旧编号污染顺序和截图归属
- 前端 review payload 已保留 complex 字段，locked source ref 不应再切回 slice
- 测试与样本核查：
  - `prototype.tests.test_complex_table_mode`
  - `prototype.tests.test_table_assets` 新增截图专项回归
  - `prototype.tests.test_unified_workspace.UnifiedWorkspaceTests`
  - `prototype.tests.test_extractor_sections`
  - 已通过：
    - `prototype.tests.test_extractor_sections`
    - `prototype.tests.test_unified_workspace.UnifiedWorkspaceTests`
    - `prototype.tests.test_complex_table_mode`
    - 新增的截图专项定向回归 `7` 条
    - 新增的 req-code/source-signature 定向回归 `7` 条
  - 真实油泵 sample spot check：`17-25` 页只剩 `10` 条 synthetic rows，且只有 `2` 个 complex zones
  - 普通截图样本 `REQ-0025 / REQ-0114 / REQ-0125` 已恢复到 `920px` 宽，高度不再被强压到 `1480`
  - 本轮新 spot check：
    - `REQ-0013` 只保留 `3.1` 标题和车型信息表
    - `REQ-0014` 不再带出 `3.1`
    - `REQ-0055` 不再带出 `8.1.3`
    - `REQ-0091` 恢复整张交付物表
    - `REQ-0106` 恢复整张“软件开发要求”表

## 当前已知问题

- `generic_complex_table` 仍只做 metadata candidate，不做 destructive replace，这是 V1 设计上的保守限制
- 还需要前端/导出人工核查，重点确认 summary 不出现在 export，group row source snapshot 可读且保存后不散掉
- 还需要前端人工核查普通非 complex source snapshot 的定位是否稳定
- 还需要前端人工核查 cache 升版后的首次 refresh 行为，确认不会继续读到修复前的 `3.2.2` 错序 preview
- 还需要前端人工核查 preview 重建后的顺序稳定性，确认内部 `req_code` 漂移不会再导致：
  - page `8/11/13` 顺序错乱
  - review 保存错配到旧条目
  - 截图和 section metadata 贴到错误行
- `prototype.tests.test_table_assets.TableAssetsParsingTest` 里还有 `4` 条老的 PDF cell spacing / 文本归一化断言未收口，不是本轮截图裁图 blocker，但后续可以转到截图专项 worktree 处理

## 下一步

- 用真实油泵样本跑普通截图前端验收，重点核：
  - `3.1 / 3.2.1 / 8.2.5.3 / 5.2 / 软件开发要求`
- 用真实油泵样本优先跑 `3.2.2` 顺序验收：
  - refresh preview 后 `1）2）3）4）5）...` 顺序应与文档一致
  - 确认前端不是在复用修复前的旧 preview cache
- 用真实油泵样本跑 preview 重建 / workspace reload 验收，重点核：
  - page `8/11/13` 的顺序
  - save 后 reload 是否还对应原条目
- 用真实油泵样本跑页 `17-25` review/workspace/export 前端验收
- 核对 workbook 中 complex group 行的图片、顺序和锁定行为
- 后续截图精度/排序类新问题优先转到 `codex/screenshot-precision-maintenance` worktree 继续维护
