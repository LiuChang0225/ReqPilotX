# Active Context

更新时间：2026-03-24 14:17 CST  
适用仓库：`/Users/liuchang/Documents/New project`

长期背景：[`/Users/liuchang/Documents/New project/docs/项目.md`](/Users/liuchang/Documents/New%20project/docs/项目.md)

## 当前目标

- 提高 oil pump PDS 的 extraction/review/export 一致性
- 当前 extraction 分支重点从 `Complex-Table Mode V1` 扩展到“截图精度恢复 + 顺序稳定性恢复”，先把普通 source snapshot 的顺序与裁图边界重新拉稳

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
  - `/Users/liuchang/Documents/New project/worktrees/_active/extraction-optimization/prototype/storage/uploads/9b0b3383c2b24ac09ac34bf24974b01e_产品设计规格说明书PDS-DE前后油泵总成-V1.1-20251215.pdf`

## 最近已修

- `Complex-Table Mode V1` 主链路已接通：
  - preview 在 enrich table assets 之前检测 complex zones
  - 页 `17-22` 作为 `merged_requirement_matrix`
  - 页 `23-25` 作为 `lifecycle_matrix`
  - 对 `complete_requirement_entries` 和 `complete_requirement_entries_raw` 都会做 zone rewrite
- 本轮 review finding 已闭环：
  - complex rewrite 现在在普通 dedupe 之后执行，synthetic rows 不再参与 `_build_section_unified_entries`
  - zone rewrite 改成 family-specific selective collapse，不再按整页误吞
  - 被吞掉的伪 row 会记录 `complex_shadow_signatures`，workspace fallback 只按这些签名精确屏蔽
  - complex row 即使复用已有 `source_snapshot_path`，保存后也会持久化 `document_table_assets`
  - `generic_complex_table` detector 已落地，但在油泵 sample 文档默认关闭，避免当前样本出现额外 candidate 噪音
- synthetic rows 行为已落定：
  - `zone_summary`：`keep=false`，只做 review 提示，不进入锁定/导出
  - `group`：`keep=true`，进入 save/apply/export
- complex row metadata 已贯通：
  - preview -> review payload -> DB JSON -> workspace reload -> export
  - DB 不加 schema，complex 顶层字段可由 `table_refs` 反向补回
- complex table refs 已强制：
  - `export_variant=source`
  - `recommended_export_variant=source`
  - `export_variant_locked=true`
  - `slice_render_mode=snapshot_only`
- workspace fallback 已加 complex 覆盖过滤：
  - 同 zone 内旧 preview/fallback 伪 requirement 只会按 swallowed signatures 被精确屏蔽
- page `13` 安全回归仍保持：
  - `REQ-0123 / REQ-0125` 当前修复未被 complex-table 改坏
- 顺手补了 table/export 兼容回归：
  - exporter 恢复旧调用签名兼容
  - row-slice bbox 规则收紧，不会把前一张表误裁进来
  - 单行 `用电器试验大纲` pipe row 又能正确 materialize 为结构表
- 已定位并修复截图回归：
  - 根因是 `source snapshot` 的总高度上限被全局应用到了普通精准裁图
  - 普通 `_write_source_snapshot` 不再按总高度缩小，精细截图恢复原始分辨率
  - 只有 full-page 跨页总览类长图继续使用高度上限
  - source snapshot 签名已 bump，preview cache 也加了版本键，旧截图缓存会判 stale
- 本轮又修了一组普通截图精度问题：
  - `10）表面处理要求以DVP为准。` 这类短句 top-level clause 会重新识别成独立 local clause，页 `8` 的顺序已恢复，不再乱入 `3.2.2`
  - 结构化大表默认不再误走 `clause-window`，`3.2.1 / 5.2 / 软件开发要求` 的 source snapshot 恢复为整段可信表图
  - `anchor-window` 的 text/image/drawing bbox 现在会先裁到窗口内，`8.2.5.3` 不再把上方 `8.1.3` 大图整块吸进来
  - 单行表格尾部在遇到下一节 heading 时允许更早收口，`3.1` 不再带出 `3.2 / 3.2.1`
- 已修复一条更底层的顺序/截图污染根因：
  - 早期 extractor 生成的内部 `req_code` 之前被 downstream 当成身份/排序主键使用
  - workspace 排序、preview overlay、review-save 匹配、preview 补 section metadata、table-assets materialize 现在统一改成 `source signature` 优先（页码/anchor/text）
  - `req_code` 只保留为弱兜底，不再在 preview 变化后把旧条目错误贴回新顺序
  - 已补 workspace 定向回归，覆盖 stale `req_code`、req_code 变化后的 overlay、save 匹配和 table-assets source-page 回填
- 已修复 `3.2.2` 局部子条目顺序仍错乱的问题：
  - 根因不在 parser/extractor，而在 preview 组装阶段把 synthetic split-clause section 重新按跨页 requirement 行号对齐
  - `4）/6.4）` 这类跨页子节不再凭后续行号跳到 `2）/3）`、`5）/6）` 前面
  - 真实样本新生成 preview 中，`3.2.2` 现在恢复为 `1）→2）→3）→4）→5）→6）→6.1）→6.2）→6.3）→6.4）...`
- preview cache 已再次升版：
  - `_PREVIEW_CACHE_VERSION` 提升到 `preview-cache-v4`
  - 旧版本 preview JSON 会自动判 stale，避免前端继续读到修复前的错序结果
- matrix 面板已新增草稿提醒：
  - 当条目集已生成但尚未锁定时，Compliance Matrix 面板会显示“未锁定条目集，非正式 Compliance Matrix”
  - 仅提示状态，不改变当前 analyze -> review -> lock 的灵活流程
- 真实油泵 sample 复核通过的关键截图：
  - `REQ-0013` 只保留 `3.1` 标题和车型信息表
  - `REQ-0014` 不再带出 `3.1`
  - `REQ-0055` 不再带出 `8.1.3`
  - `REQ-0091` 恢复整张交付物表
  - `REQ-0106` 恢复整张“软件开发要求”表
- 真实油泵 sample spot check：
  - preview 只剩 `2` 个 complex zones
  - 页 `17-25` 现在只有 `10` 条 synthetic rows，`0` 条 non-complex rows
  - 普通截图样本如 `REQ-0025 / REQ-0114 / REQ-0125` 已恢复到 `920px` 宽

## 当前 blockers

- 后端主链路无新的截图 blocker
- 旧文档首次打开时可能因为 cache 升版显示 `STALE`，需要触发一次 preview refresh 才会拿到新的顺序结果
- 还缺前端人工 UI/导出 spot check，重点看两块：
  - 页 `17-25` 的 review、reload 和 workbook 表现
  - 普通非 complex source snapshot 在 review / workspace / export 中的位置、顺序和清晰度是否稳定
- 还需要前端人工确认一轮“preview 重建后内部 req_code 漂移”的场景：
  - review 刷新后顺序不应乱
  - 已保存条目不应因为旧 req_code 被错配到别的 preview 行
  - 截图/section metadata 不应被错贴到同页其他条目
- `prototype.tests.test_table_assets.TableAssetsParsingTest` 里仍有 `4` 条老的 sample-based 文本归一化断言会因 PDF cell spacing 差异失败；它们不是本轮截图裁图回归的主因，但后续可在截图专项 worktree 顺手收口

## 下一步

- 在真实油泵样本里手动核普通截图：
  - `3.1 / 3.2.1 / 8.2.5.3 / 5.2 / 软件开发要求` 在 review UI、workspace reload、export workbook 中都表现正常
- 在真实油泵样本里优先手动核 `3.2.2`：
  - preview refresh 后 `1）2）3）4）5）...` 顺序应与文档原文一致
  - 确认前端没有继续使用旧 preview cache
- 在真实油泵样本里手动核一轮 preview 重建后的顺序稳定性：
  - 重点关注 page `8/11/13`
  - 确认 reload 后 `display_req_code` 只反映当前锁定集顺序，内部 `req_code` 不再影响展示顺序或截图归属
- 在真实油泵样本里继续核页 `17-25`：
  - preview / review / workspace reload 一致
  - summary 不进 apply/export
  - group row reload 后不丢 complex metadata 与 source-locked ref
- 后续若还有新的截图精度/排序问题，优先在 `codex/screenshot-precision-maintenance` worktree 里收敛，避免继续把 extraction 主分支搅脏

## 活跃端口

- `8015`：当前 extraction/review 本地核查端口
