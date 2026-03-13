# Extraction Active Context

更新时间：2026-03-13 12:12 CST  
分支：`codex/extraction-optimization`

## 1. 当前目标

只做两件事：

1. 把条目拆分做好
2. 不出现重复、前后混乱、假表头

## 2. 当前样本

- `/Users/liuchang/Documents/New project/worktrees/_active/extraction-optimization/prototype/storage/uploads/9b0b3383c2b24ac09ac34bf24974b01e_产品设计规格说明书PDS-DE前后油泵总成-V1.1-20251215.pdf`

## 3. 当前已修

1. 远距离重复编号全局合并已禁
2. 后半段局部 `1/2/3` 重新编号已切到 `SUB1` 作用域
3. `2 油口接头接口根据需求进行定义` 不再错误挂到主文 `2`
4. `3.2.1 产品信息`：
   - 假表头已禁
   - 真实表头已恢复
   - 三张表已收回一张
5. `3.2.2`：
   - `8015` / API 中，`1）/2）/5）/6.6.1~6.6.5` 已归到 `3.2.2 产品详细描述`
6. `SUB1`：
   - `Appendix SUB1` scope 不再被截成单字母
   - `REQ-0122` 不再错误挂到页 30 的 `4 解锁力 / Unlock force`
7. requirement 章节显示：
   - `3.2.2` 下条目在 `8015` 工作台/API 中已显示自己的 section，不再回退成父级 `3.2`
8. 附件边界：
   - `7、附件目录` 已恢复为正文最后一节
   - 页 11 起已切入 `SUB1`，不再继承正文 `6 信息安装需求`
9. 无编号附件标题页：
   - `软件开发要求`、`FMEA与特殊特性`、`技术要求内容`、`网络安全`、`数据安全` 已能起独立标题块
10. 附件内顶层 heading：
   - `2 特殊特性` 已恢复
   - `4 OTA设计要求` 已恢复
11. 本轮新增：
   - 页 14 续表不再误起孤立标题 `2`
   - `SUB1` 内重复顶层编号已拆成不同 `section_instance_key`，`可靠性设计目标` 不再串成 `FMEA要求`
   - 页 23 生命周期矩阵已选回 `pymupdf4llm` 结构化页
   - 页 16 现保留 `附件4 --OTA内容` 左栏标签块
   - `8015`/workspace 已确认不再把后半段挂回页 30 的 `4 解锁力`
12. 本轮继续新增：
   - 页 15 现优先选回 `pypdf`，`附件4--诊断、OTA相关标准` 与 `附件4 --诊断要求` 已在 review 流起独立 heading
   - `3.1 诊断设计要求` 保住了 `（9）`，不再被附件带标题切坏
   - 页 23-25 生命周期矩阵现可继承前序总表头，`概念设计 / 开发 / 生产 / 运营维护 / 报废终止支持` 已全部转成 `structured`
   - 新增通用规则：
     - 行内 `附件X --标题 + 正文` 自动拆线
     - 稀疏矩阵切片允许复用前序宽表头，并过滤纯占位噪声行
13. 本轮新增：
   - `10）表面处理要求以DVP为准。` 已恢复
   - 新增“源 PDF 表格回收”：
     - `3.1 车型信息`
     - `5.2 交付物要求`
     - `7 附件目录`
     - `8.1.3`
     - `8.2.5.3`
   - `HEADING` 条目不再挂表资产
   - source crop 现对 row-slice 继续套用前后锚点，`3.1 / 3.2.1 / 5.2 / 7附件目录` 原图裁剪已收紧
   - 页 26/27 已拆成 3 张结构表：密码算法 / Confidentiality Level / RASIC Definition
14. 本轮继续新增：
   - source recovery 现加入“当前条目自身锚点”与“当前表头/首行特征必须命中”的约束，先锁当前条目再选候选表
   - `5.2 交付物要求` 现回收到页 10 的正确小表，不再串到同页 `6 / 7` 大候选
   - `7、附件目录` 现能收回完整 12 行，不再只留前几行
   - `8.2.5.3` 现锁定 `动刚度低频测试条件` 小表，不再误拿 `8.1.3`
   - `8.1.3` 现支持“连续 title band + 单位行 + 真表头”模式，位移表已能结构化
   - leading noise / title band（如 `6.4）低压电气接口`）不再污染 recovered header
   - 已在 `prototype/tests/test_table_assets.py` 补真实油泵样本回归：
     - `5.2`
     - `7 附件目录`
     - `8.1.3`
     - `8.2.5.3`
     - `6.4` 类 title-band header 污染
15. 本轮新增：
   - 长条目 clause 锚点新增“短标题前缀”变体，block 内没有邻居时会回退到前后条目 context，after anchor 也加了最低置信门槛
   - `3）油泵总成接口要求` 已恢复 `图示` 列，并且 source snapshot 终点能守在 `4）外观要求` 之前
   - `6.4）低压电气接口` 已回到干净表头 `序号/定义/描述/备注`，source snapshot 不再串到 `6.5 / 6.6.x`
   - `6.7）功能安全要求` 现能从源 PDF 回收安全目标表，空白占位首列已去掉
   - `7.2）环境适应性要求` 现会保留 `snapshot_only` 原图；`7.1 / 7.3 / 9.2 / 4）外观要求` 已不再误挂截图
   - `8.1.2 / 8.1.3` 已在 review 流转成 `structured`，并重新生成了 source snapshot
   - 另发现 `review_entries` 会覆盖回 DB 里旧的 `table_refs`；现已改成 review overlay 优先保留 preview 自己的 `table_refs/content_blocks`，只吸收持久化 `export_variant`
16. 本轮新增：
   - `source-crop-v19` 已落地，专门修“表已识别对，但原文截图只截到上半段”
   - 行切片匹配现对尾行使用“从后往前”的最后命中，避免 `3.2.1` 这类尾行文案重复时只截到前半张表
   - row-slice 一旦命中：
     - 不再让 start anchor 把表头/title band 顶掉
     - 不再让 next clause end anchor 提前砍掉最后几行
   - 已实测收紧/放全：
     - `3.2.1`
     - `5）`
     - `6.1）`
     - `6.3）`
     - `6.4）`
     - `6.5）`
     - `8.1.2）`
   - `7、附件目录` 的 source snapshot 已能显示完整总表；review 顺序现稳定为 `7 附件目录 -> FMEA -> 附件4...`
17. 本轮新增：
   - `source-crop-v23` 已落地，继续修 `snapshot_only` 与附件末页串图
   - snapshot-only 现新增三条规则：
     - 顶部优先贴当前 clause 标题，不再把上一条表尾/边框带进来
     - 优先用“下一条 local clause 标题”作为 end anchor，不再被更靠后的正文行抢权
     - synthetic clause end anchor 不再复用 table candidate cache，避免 live preview 中读到旧表缓存
   - 结构表末页现会跳过“只命中弱 note”的候选页，页 `26/27` 第一张密码算法表已不再串到 `Confidentiality Level`
   - 已实测：
     - `6.2）功能框图` 不再带上一条表尾
     - `7.2）环境适应性要求` 原图终点已守在 `7.3）机械性能要求` 之前
   - Playwright 已确认：
     - `软件开发要求` 行 DOM 中无图片
     - `常见不安全密码算法` 行已挂到新的 3 张 `v23` source snapshot URL
18. 本轮新增：
   - preview refresh / analyze 现会清当前文档 preview 截图根目录；persisted table-assets 改为独立根目录，不再和 preview 共用 `table_snapshots/<document_id>`
   - `/snapshot` 与 `/source-snapshot` 现统一回 `no-store` 响应头，浏览器不会再保留旧图
   - `table-assets` 接口已改成 preview 资产优先，不再先读 DB 里的旧资产；review 页附件区 source-snapshot 404 已收住
   - 标准 markdown 表的 `| --- | --- |` 分隔行不再被 `_split_text_blocks` 拆成 paragraph，正常 markdown 表可保持单表渲染
   - `8.1.2` 现会把竖向合并的目标列重新分配到正确行：
     - `Lateral -> 避开 40-60Hz`
     - `Bounce -> 38-45Hz`
     - `Roll -> 70-95Hz`
     - `Pitch -> 120-180Hz`
   - 页 26/27 的 `Confidentiality Level` 与 `RASIC Definition` 不再错误继承前序 16 列网络安全矩阵表头，现分别回到 2 列结构表
   - 真实油泵 preview 已重刷到 `2026-03-13 09:23 CST`
   - Live 核查：
     - `软件开发要求` 行仍是纯 heading
     - `tbl_12986ef35782 / tbl_5e0a0c1b1e04 / tbl_c74f2768646f` 的 source-snapshot 已返回 `200`
19. 本轮继续新增：
   - preview / persisted 截图目录现已彻底分离：
     - preview：`table_snapshots/preview/<document_id>`
     - persisted：`table_snapshots/persisted/<document_id>`
   - `_sync_document_table_assets` 只会清 persisted 根目录，不会再误删 preview 资产
   - source-snapshot 现改成原子写入；若磁盘上已有 0 字节 / 空白坏图，会自动重渲染，不再继续复用
   - 油泵附件页 `tbl_12986ef35782 / tbl_5e0a0c1b1e04 / tbl_c74f2768646f` 的 preview source-snapshot 已在真实文档里重建为正常图片，不再缺文件或白条
20. 本轮继续新增：
   - `REQ-0125 集成验证要求` 的 `PDS表位置/内容` 脏尾巴已从 `content_blocks` 去掉
   - 对“表后最后一个紧凑目录头/列头带”新增尾噪声规则：
     - source-snapshot 的 after-context 会回退到下一条真实 heading
     - 非结构化 `snapshot_only` 表块会强制改走 clause-window crop
   - 结果：
     - `REQ-0125` source snapshot 已守在页 14，不再带页 15 黄色目录带
     - `REQ-0127` 保持正确诊断页裁剪
   - 当前 preview 复查：`PDS表位置内容` 在 review blocks 中已为 `0` 条；`missing asset = 0`

21. 本轮继续新增：
   - `display_text/raw_text` 现会从 `content_blocks + table_refs` 重新生成，已清掉：
     - `4）外观要求` 的重复尾段
     - `6.4）` 的 `Col4`
     - `2.2` 的 `#REF!`
     - 附件二列表的重复表头回灌
   - source-first 推荐新增两类：
     - `2.3 特殊特性控制要求` 这类单行长阈值表
     - `8.1.3` 这类 `X/Y/Z/RX/RY/RZ` 轴向位移表
   - live `8015` 已确认：
     - `REQ-0114` 现为 `source/source`
     - `REQ-0046` 现为 `source/source`
     - `REQ-0165` 的 `Confidentiality Level / RASIC` 在 workspace API 输出中已不再重复表头
   - review 页：
     - 已有 `全部保留 / 全部移除`
     - 切换 `表格/原文截图` 时不再整表重绘；Playwright 实测点击 `2.3` 会定位到 PDF 第 `12` 页

## 4. 当前待修

1. 页 15 目录页本身还是 `snapshot_only`，后续可继续压目录表结构化与原图保真
2. 页 15 `27服务认证设计规范` 仍在目录页引用清单内，目录项和正文项边界还可继续清理
3. 页 17-27 的后半段附件虽然大体顺序已回正，但还要继续压更深层错挂/误升章节
4. `6.6.1` 的原图顶部仍带少量 heading band / 页内边框，可继续压更紧
5. `3）/6.4）` 的 source crop 已基本守住边界，但顶部仍有很少的前文噪声行
6. 页 `26/27` 的最后一项 source snapshot 已换新，但是否完全满足交付观感还建议再做一次人工目检
7. `prototype.tests.test_unified_workspace -v` 仍有一批旧失败（含 exporter 参数签名与若干 row-slice 老断言），和本轮 preview/source-snapshot 修复无直接关系，后续单独清

## 5. 明确禁止

1. 禁止假表头 fallback
2. 禁止远距离同码章节全局合并
3. 禁止把表格序号行当真章节
4. 禁止为了“看起来整齐”而重排原文顺序

## 6. 当前关键文件

- `/Users/liuchang/Documents/New project/worktrees/_active/extraction-optimization/prototype/app/services/extractor.py`
- `/Users/liuchang/Documents/New project/worktrees/_active/extraction-optimization/prototype/app/services/extraction_preview.py`
- `/Users/liuchang/Documents/New project/worktrees/_active/extraction-optimization/prototype/app/services/table_assets.py`
- `/Users/liuchang/Documents/New project/worktrees/_active/extraction-optimization/prototype/app/db.py`

## 7. 当前测试

已存在专项测试：

- `/Users/liuchang/Documents/New project/worktrees/_active/extraction-optimization/prototype/tests/test_extractor_sections.py`
- `/Users/liuchang/Documents/New project/worktrees/_active/extraction-optimization/prototype/tests/test_parser_merge.py`
- `/Users/liuchang/Documents/New project/worktrees/_active/extraction-optimization/prototype/tests/test_table_assets.py`

本轮已跑：

- `python3 -m unittest prototype.tests.test_table_assets -v`
- `python3 -m py_compile prototype/app/services/table_assets.py prototype/tests/test_table_assets.py`
- `PYTHONPATH=prototype ./prototype/.venv/bin/python -m unittest prototype.tests.test_unified_workspace.UnifiedWorkspaceTests.test_refresh_preview_endpoint_builds_and_persists_cache prototype.tests.test_unified_workspace.UnifiedWorkspaceTests.test_preview_refresh_purges_stale_snapshot_root_before_rebuild prototype.tests.test_unified_workspace.UnifiedWorkspaceTests.test_table_asset_api_returns_asset_and_snapshot prototype.tests.test_unified_workspace.UnifiedWorkspaceTests.test_table_asset_api_exposes_source_snapshot_for_pdf_page_span prototype.tests.test_unified_workspace.UnifiedWorkspaceTests.test_table_asset_api_prefers_preview_asset_over_stale_db_asset -v`
- `PYTHONPATH=prototype ./prototype/.venv/bin/python -m unittest prototype.tests.test_table_assets.TableAssetsParsingTest.test_write_source_snapshot_regenerates_corrupt_existing_file prototype.tests.test_unified_workspace.UnifiedWorkspaceTests.test_preview_refresh_purges_stale_snapshot_root_before_rebuild prototype.tests.test_unified_workspace.UnifiedWorkspaceTests.test_sync_document_table_assets_keeps_preview_snapshot_root_intact prototype.tests.test_unified_workspace.UnifiedWorkspaceTests.test_table_asset_api_prefers_preview_asset_over_stale_db_asset prototype.tests.test_unified_workspace.UnifiedWorkspaceTests.test_table_asset_source_snapshot_falls_back_to_persisted_asset_when_preview_source_is_missing -v`
- `PYTHONPATH=prototype ./prototype/.venv/bin/python -m unittest prototype.tests.test_table_assets -v`
- `./prototype/.venv/bin/python -m py_compile prototype/app/services/preview_cache.py prototype/app/main.py prototype/app/services/table_assets.py prototype/tests/test_unified_workspace.py prototype/tests/test_table_assets.py`

当前环境限制：

- `prototype.tests.test_parser_merge`
- `prototype.tests.test_extractor_sections`
  - 受 `python-docx` 缺失影响，当前无法直接导入
- 当前 shell 自带 Python 仍缺 `python-docx / pypdf`，全量 parser/extractor 回归需要继续走 `.venv`

当前服务：

- `http://127.0.0.1:8015/workspace?stage=review`

## 8. 操作约定

每次修完一个明显问题后，更新本文件：

- 问题
- 根因
- 修复点
- 是否回归通过

保持短，不写大而全背景。长期背景统一看：

- `/Users/liuchang/Documents/New project/docs/active-context.md`

## 9. 新窗口启动提示词

如果要在 Codex 新窗口继续这一条线，建议直接使用：

`继续处理油泵 PDS 的拆解问题。先读取 /Users/liuchang/Documents/New project/docs/active-context.md 和 /Users/liuchang/Documents/New project/worktrees/_active/extraction-optimization/prototype/docs/extraction-active-context.md，然后检查 8015 上 3.2.2、6.6.1~6.6.5、后半段顺序和列表项误升章节问题。`

## 9. 下一步

1. 继续按你原始问题单回点 review 页，优先复查 `6.6.1 ~ 6.6.3` 与附件尾页
2. 如果上午交付前还要再压质量，优先人工目检页 `26/27` 最后一项和附件尾页，而不是继续改 source-crop 主链
3. 如果 review 页再次出现旧截图/404，先检查 `preview_cache.py` 的 snapshot purge 是否生效，以及 `main.py` 的 `get_document_table_asset` 是否仍走 preview 优先
