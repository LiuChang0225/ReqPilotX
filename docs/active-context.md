# Active Context

更新时间：2026-03-13 12:12 CST  
适用仓库：`/Users/liuchang/Documents/New project`

这份文档用于压缩会话上下文，给后续 Codex 新窗口/新线程做快速接续。  
长期背景看：`/Users/liuchang/Documents/New project/docs/项目.md`  
短期状态看：本文件。

## 1. 当前项目目标

当前阶段的最高优先级只有一个：

1. `条目拆分准确`

具体要求：

- 不重复
- 不前后错乱
- 章节归属正确
- 表格识别正常
- 人工审阅时能信任原文/原图

## 2. 当前主线与分支

主仓库：

- `main`

活跃 worktree：

1. `codex/table-assets-mainline`
   - 目录：`/Users/liuchang/Documents/New project/worktrees/_active/table-assets-mainline`
   - 职责：表格资产化主线，Web/Excel 优先消费同一份表格资产

2. `codex/workspace-stabilization`
   - 目录：`/Users/liuchang/Documents/New project/worktrees/_active/workspace-stabilization`
   - 职责：统一工作台稳定性、性能、虚拟滚动、Windows 卡顿

3. `codex/table-asset-lifecycle`
   - 目录：`/Users/liuchang/Documents/New project/worktrees/_active/table-asset-lifecycle`
   - 职责：保留原图预生成，但做资产瘦身、去重、保留策略、生命周期管理

4. `codex/extraction-optimization`
   - 目录：`/Users/liuchang/Documents/New project/worktrees/_active/extraction-optimization`
   - 职责：文档拆解精度专项，当前重点样本是“油泵 PDS”

## 3. 当前真实重点样本

当前正在修的文档：

- `/Users/liuchang/Documents/New project/worktrees/_active/extraction-optimization/prototype/storage/uploads/9b0b3383c2b24ac09ac34bf24974b01e_产品设计规格说明书PDS-DE前后油泵总成-V1.1-20251215.pdf`

当前专项重点：

- `3.2.2 产品详细描述`
- 油泵 PDS 后半段附件流/内嵌子文档流（重新从 `1/2/3/4...` 编号开始）
- 表格续页、附件左栏标签、复杂生命周期矩阵

## 4. 当前已确认的问题与状态

### 已修

1. 远距离重复编号全局合并
   - 后半段局部 `1/2/3` 不再吞掉主文前半段

2. 扁平化表格行误升章节
   - 如 `2 油口接头接口根据需求进行定义` 不再错误挂到 `2 供应商能力要求及设计责任`

3. 假表头
   - 已禁止通用测试矩阵假表头 fallback
   - 不允许再出现“序号/项目/参考标准/试验方法/DV-PV/责任方...”这类不属于原文的表头

4. `3.2.1` 一张表被切成多张
   - 当前已经恢复为 1 张表
   - 表头已恢复为真实字段

### 已修（新增）

1. `3.2.2` 归属链
   - `8015` 工作台/API 中，`1）/2）/5）/6.6.1~6.6.5` 已显示为 `3.2.2 产品详细描述`

2. `SUB1` scope 截断
   - `Appendix SUB1` 不再被截成单字母 scope
   - 后半段 `REQ-0122` 不再错误挂到页 30 的 `4 解锁力 / Unlock force`

3. `3.2.2` 工作台显示父章节
   - 页面“章节”列现在优先显示 requirement 自己的实际 section，而不是父级 `3.2`

4. 附件流起点修正
   - `7、附件目录` 已保留为正文最后一节
   - 页 11 起已切入 `SUB1` 附件流，不再挂在正文 `6 信息安装需求`

5. 无编号附件标题页
   - 页 11 `软件开发要求`
   - 页 12 `FMEA与特殊特性`
   - 页 17 `技术要求内容`
   - 页 20 `网络安全`
   - 页 21 `数据安全`
   - 以上页面已能起出独立标题块，不再整页吞进上一节

6. 附件内顶层 heading 回收
   - `2 特殊特性` 已恢复
   - `4 OTA设计要求` 已恢复

7. 续表/实例键/复杂矩阵（本轮新增）
   - 页 14 不再误起孤立标题 `2`，`集成验证要求` 现从页 13 连续到页 14
   - `SUB1:1` 这类同 scope 重复编号已拆成不同 `section_instance_key`，`可靠性设计目标` 不再串成 `FMEA要求`
   - 页 23 生命周期矩阵已重新选回 `pymupdf4llm` 结构化页，不再退回碎裂的 `pdfplumber`
   - 页 16 现保留 `附件4 --OTA内容` 左栏标签块
   - `8015` / workspace 中已确认不再出现把后半段挂回 `4 解锁力 / Unlock force` 的脏顺序

8. 附件带标题与跨页矩阵（本轮新增）
   - 页 15 现优先选回 `pypdf`，`附件4--诊断、OTA相关标准` 与 `附件4 --诊断要求` 已在 `8015` review 流中起出独立 heading
   - `3.1 诊断设计要求` 不再因为附件标题串线而吞掉/错挂 `（9）`
   - 页 23-25 生命周期矩阵已支持继承前序总表头，`概念设计 / 开发 / 生产 / 运营维护 / 报废终止支持` 现都能落 `structured`
   - 新增两条通用规则：
     - 行内 `附件X --标题 + 正文` 自动拆线
     - 稀疏矩阵切片可复用前序宽表头，并丢弃纯占位噪声行

9. 源 PDF 表格回收与截图裁边（本轮新增）
   - `10）表面处理要求以DVP为准。` 已恢复，不再在页 8 的本地 clause 流中被短文本过滤掉
   - 对“无 pipe、但源 PDF 明确有表”的条目新增源表回收：
     - `3.1 车型信息`
     - `5.2 交付物要求`
     - `7 附件目录`
     - `8.1.3`
     - `8.2.5.3`
   - `HEADING` 行不再误挂表资产
   - source crop 现会在 row-slice 之外继续受前后锚点约束，`3.1 / 3.2.1 / 5.2 / 7附件目录` 的原图起止已明显收紧
   - 页 26/27 现可拆出三张结构表：
     - 密码算法表
     - Confidentiality Level
     - RASIC Definition

10. 源表回收第二刀（本轮新增）
   - source recovery 现加入“当前条目自身锚点”与“当前表头/首行特征必须命中”的双约束，优先锁当前条目，不再靠上一张表抢分
   - 页 10 `5.2 交付物要求` 现回收到正确小表，不再串到同页 `6/7` 大杂糅候选
   - 页 10 `7 附件目录` 现保留完整 12 行，不再只收前几行
   - 页 8 `8.2.5.3` 现锁定 `动刚度低频测试条件` 小表，不再误拿 `8.1.3`
   - 页 8 `8.1.3` 现支持“连续标题带 + 单位行 + 真表头”模式，位移表可转成结构化行
   - leading noise / title band（如 `6.4）低压电气接口`）不再污染 recovered header

11. 油泵 PDS 局部表/截图第三刀（本轮新增）
   - 长条目 clause 锚点现补“短标题前缀”变体，block 内无邻居时会回退到前后条目 context，end anchor 也加了最低置信门槛
   - `3）油泵总成接口要求` 现恢复 `图示` 列，review 流里已转成 4 列结构表，source crop 终点守在 `4）外观要求` 之前
   - `6.4）低压电气接口` 现回到干净表头 `序号/定义/描述/备注`，不再把 `6.5` 当 notes，source crop 终点已压回本条
   - `6.7）功能安全要求` 现能从源 PDF 回收安全目标表，空白占位首列已去掉，review 流已转成 `structured`
   - `7.2）环境适应性要求` 现保留 `snapshot_only` 原图，不再被当纯文本丢掉
   - `7.1）/7.3）/9.2）/4）外观要求` 这类纯文本条目已在 review 流去掉误挂截图
   - `8.1.2 / 8.1.3` 现都已在 review 流落成 `structured`，并重新生成 source snapshot，原图位置不再漂到后续条目
   - 刷新 preview 后另发现 `review_entries` 会叠加 DB 里的旧 `table_refs`；现已改成 review overlay 优先保留 preview 自己的 `table_refs/content_blocks`，只吸收持久化 `export_variant`

12. 结构化截图裁剪第四刀（本轮新增）
   - `source-crop-v19` 已落地，专门修“表已识别对，但原文截图只截到上半段”
   - 行切片匹配现对尾行使用“从后往前”的最后命中，避免 `3.2.1` 这类尾行文案重复时只截到前半张表
   - row-slice 一旦命中：
     - 不再让 start anchor 把表头/title band 顶掉
     - 不再让 next clause end anchor 提前砍掉最后几行
   - 已实测收紧/放全：
     - `3.2.1 产品信息`
     - `3.2.2 / 5）油管总成性能要求`
     - `3.2.2 / 6.1）简述`
     - `3.2.2 / 6.3）功能`
     - `3.2.2 / 6.4）低压电气接口`
     - `3.2.2 / 6.5）电气性能要求`
     - `8.1.2）` 表头/表顶已完整保住
   - 附件页核查：
     - `7、附件目录` 的 source snapshot 已可正确显示完整总表
     - review 顺序现稳定为 `7 附件目录 -> FMEA -> 附件4...`，刷新后不再把旧截图串回后续附件

13. source crop 第五刀（本轮新增）
   - `source-crop-v23` 已落地，继续收 `snapshot_only` 与附件末页串图
   - 新增规则：
     - snapshot-only 顶部优先贴当前 clause 标题，不再把上一条表尾/边框带进来
     - snapshot-only 优先用“下一条 local clause 标题”作为 end anchor，不再被更靠后的正文行抢权
     - synthetic clause end anchor 不再复用 table candidate cache，避免 live preview 中误读旧表缓存
     - 结构表末页若只命中弱 note 候选，会直接跳过该页，避免串到下一张表
   - 已实测收住：
     - `6.2）功能框图` 不再带上一条表尾
     - `7.2）环境适应性要求` 原图终点已守在 `7.3）机械性能要求` 之前
     - 页 `26/27` `常见不安全密码算法` 第一张 source snapshot 不再误接 `Confidentiality Level`
   - Playwright 实测：
     - `软件开发要求` 行 DOM 中无图片，不再出现 FMEA 旧图回灌
     - `常见不安全密码算法` 行已挂到新的 3 张 `v23` source snapshot URL

14. 预览缓存与 markdown 表修复（本轮新增）
   - preview refresh / analyze 现会清当前文档的 preview 截图根目录；persisted table-assets 改为独立根目录，不再和 preview 共用 `table_snapshots/<document_id>`
   - `/snapshot` 与 `/source-snapshot` 现统一回 `Cache-Control: no-store`，浏览器侧历史截图不会再被复用
   - `table-assets` 接口现改为 preview 资产优先，不再先读 DB 里的旧资产；附件区之前的 source-snapshot 404 已收住
   - 标准 markdown 表的 `| --- | --- |` 分隔行不再被拆成段落，正常 markdown 表会保持单表渲染
   - `8.1.2` 的竖向合并目标列已重新分配到正确行：
     - `Lateral -> 避开 40-60Hz`
     - `Bounce -> 38-45Hz`
     - `Roll -> 70-95Hz`
     - `Pitch -> 120-180Hz`
   - 页 26/27 的 `Confidentiality Level` 与 `RASIC Definition` 不再错误继承前序 16 列宽表头，现分别回到 2 列结构表
   - 最新油泵 preview 已重刷到 `2026-03-13 09:23 CST`
   - Live 核查：
     - `软件开发要求` 在 review 中仍是纯 heading，无图
     - 之前报错的 `tbl_12986ef35782 / tbl_5e0a0c1b1e04 / tbl_c74f2768646f` source-snapshot 现已返回 `200`
   - 本轮继续新增：
     - preview / persisted 截图目录现已彻底分离：
       - preview：`table_snapshots/preview/<document_id>`
       - persisted：`table_snapshots/persisted/<document_id>`
     - `_sync_document_table_assets` 只会清 persisted 根目录，不会再误删 preview 资产
   - source-snapshot 现改成原子写入；若磁盘上已有 0 字节 / 空白坏图，会自动重渲染，不再继续复用
   - 油泵附件页 `tbl_12986ef35782 / tbl_5e0a0c1b1e04 / tbl_c74f2768646f` 的 preview source-snapshot 已在真实文档里重建为正常图片，不再缺文件或白条

15. 目录脏尾巴与跨页截图收尾（本轮新增）
   - `REQ-0125 集成验证要求` 的 `PDS表位置/内容` 脏尾巴已从 `content_blocks` 中剔除，不再把附件目录表头当成本条正文
   - 对“表后最后一个紧凑目录头/列头带”新增尾噪声识别：
     - source-snapshot 的 after-context 遇到这类尾巴时，会回退到下一条真实 heading
     - 非结构化 `snapshot_only` 表块若检测到这类尾巴，会强制走 clause-window crop
   - 实际结果：
     - `REQ-0125` 的 source snapshot 现已守在页 14，不再带页 15 黄色目录带
   - `REQ-0127` 仍保持正确的诊断页裁剪
   - 当前 preview 复查：`PDS表位置内容` 在 review blocks 中已为 `0` 条

16. 上午交付前的文本/推荐/UI 收尾（本轮新增）
   - `display_text/raw_text` 现会从 `content_blocks + table_refs` 重新物化，自动清掉：
     - `4）外观要求` 这类重复尾段
     - `6.4）` 中的 `ColX`
     - `2.2 特殊特性分类与标识` 里的 `#REF!`
     - 附件页二列表回灌的重复表头
   - source-first 推荐新增两类：
     - `2.3 特殊特性控制要求` 这类单行长阈值表
     - `8.1.3` 这种 `X/Y/Z/RX/RY/RZ` 轴向位移表
   - live oil-pump preview 已确认：
     - `REQ-0114` 现为 `推荐截图 1/1 / 当前截图 1/1`
     - `REQ-0046` 现默认整表截图
     - `REQ-0165` 的 `Confidentiality Level / RASIC` 文本重复表头已在 workspace API 输出中清掉
   - review 页交互：
     - `全部保留 / 全部移除` 已落地
     - 切换 `表格/原文截图` 保存时不再整表重绘，点击 `2.3` 时 Playwright 已实测定位到 PDF 第 `12` 页

### 正在修

1. `3.2.2` 复杂混排
   - 继续稳住“标题 + 图/表 + 正文”的边界

2. 后半段附件流仍有脏锚点
   - 页 15 的附件目录表页本身仍主要走 `snapshot_only`，后续可继续压“目录表结构化 vs 原图保真”的取舍
   - 页 15 `27服务认证设计规范` 虽已留在目录页内，但引用清单与正文边界还可继续清理

3. 更后面的表格附件
   - 页 26/27 已拆离前文，但密码算法表、RASIC/保密级别页仍需继续压结构化切片质量
   - 少量条目的 `display_text/raw_text` 仍残留 `ColX` 占位串；当前 table assets / source snapshot 已正常，但若后续要继续抛光纯文本观感，可单独清这条链

4. 仍待继续清的正文表
   - `6.6.1` 的原图顶部仍带少量 heading band / 页内边框，属于次优先级抛光
   - `3）/6.4）` 的 source crop 现在已基本守住边界，但顶部仍会带极少量前文噪声行，属于次优先级抛光

5. 仍待继续复查的原始问题单
   - 页 26/27 的密码算法/保密级别/RASIC 页主问题已修，但附件尾页仍要继续按你原始清单逐条人工回点
   - workspace 全量单测仍有一批旧失败（含 exporter 参数签名与若干 row-slice 老断言），和本轮 preview/source-snapshot 修复无直接关系，后续单独清
   - `6.6.1 / 6.6.2 / 6.6.3` 的截图首尾与插入位置还要继续在 review UI 里复核
   - `4 内容重复一遍` 这一条还没重新确认是否已被别的修复顺带解决
6. 当前回归环境限制
   - `prototype/tests/test_table_assets.py` 已补真实油泵样本回归并通过
   - `prototype/tests/test_unified_workspace.py` 已可在 worktree `.venv` + `PYTHONPATH=prototype` 下跑通新增 preview/table-asset 回归
   - 当前 shell 自带 Python 仍缺 `python-docx / pypdf`，全量 parser/extractor 回归需要继续走 `.venv`

## 5. 当前明确的产品/工程决策

1. 人类展示优先信任原文截图/原表切片
2. AI 不读截图，AI 读结构化切片文本
3. 条目集锁定后才能导出和跑 AI
4. 锁定后继续编辑会自动解锁
5. 假表头禁止
6. 当前阶段优先级：拆解准确 > 表格花哨渲染 > AI 判定

## 6. 上下文压缩工作法

建议以后都按两层文档维护：

1. 长期背景文档
   - `docs/项目.md`
   - 放架构、数据流、模块、长期计划

2. 滚动上下文文档
   - `docs/active-context.md`
   - 只放：
     - 当前样本
     - 当前问题
     - 最近已修
     - 下一步
     - 当前分支/端口/路径

控制规则：

- 长期文档：少改，只在架构或主流程发生变化时更新
- 滚动文档：每次阶段性修复后更新，保持在 150 行内
- 新窗口接手时，先看 `docs/active-context.md`，再看对应分支下的专项上下文
- 仓库已加 `$context-sync` skill + pre-commit 门禁，代码/核心文档变更时应同步更新至少一个 active-context 文件

## 7. 新窗口/新线程怎么接续

推荐方式：

1. 打开对应 worktree
2. 先读：
   - `/Users/liuchang/Documents/New project/docs/active-context.md`
   - 对应分支下的专项文档
3. 再看 `git status`
4. 最后启动服务/执行测试

示例：

- 拆解优化窗口：
  - 目录：`/Users/liuchang/Documents/New project/worktrees/_active/extraction-optimization`
  - 先看：`/Users/liuchang/Documents/New project/worktrees/_active/extraction-optimization/prototype/docs/extraction-active-context.md`

## 8. 当前端口

- `8015`
  - 分支：`codex/extraction-optimization`
  - 用途：油泵 PDS 拆解优化测试

## 9. 下一步

1. 继续复查页 15-27
   - 保持附件 heading / 正文 / 目录顺序稳定
   - 重点看页 17-27 的后续附件是否还有脏锚点

2. 继续压页 26/27 及更后面的附件表
   - 目标是继续抛光密码算法表、RASIC/保密级别页的结构化切片与原图边界

3. 回到页 15-27 的目录页与附件深层错挂
   - 特别是 `27服务认证设计规范` 边界、`3.2.1` 窜列、`3.2.2 / 3）` 的 `图示` 列、以及其余附件尾页错挂

4. 稳定后，把有效修复回灌到：
   - `codex/table-assets-mainline`
   - 再决定是否回主线
