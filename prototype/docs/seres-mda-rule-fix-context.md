# SERES MDA Rule Fix Context

更新时间：2026-04-24 13:26 CST
分支：`codex/extraction-quality-loop`
适用范围：`SERES MDA` 系列客户文档的 extraction 审计、通用修规则、验证证据沉淀

## 文档定位

- 这不是一个短期 handoff，也不是一次性调试摘要。
- 这份文档定义为 SERES MDA 系列的长期规则台账，负责沉淀：
  - 文档清单与结构画像
  - 发现过的所有问题
  - 问题所属层级与根因判断
  - 已升格的通用修复规则
  - 修复后的有效性证据
  - 当前残余与不确定项
- `docs/active-context.md` 与 `prototype/docs/extraction-active-context.md` 只保留当前状态、端口、下一步，不重复这里的长账本内容。
- 后续任何 SERES 文档再出现新问题，优先更新本文件，而不是把细节散落在聊天记录或临时 handoff 中。

## 更新要求

- 新增 SERES 文档时，先补到“文档清单与结构画像”。
- 新发现问题时，至少记录：
  - 受影响文档
  - section / page / 图表位置
  - 现象
  - 根因层判断：`parser / extractor / preview / gate / profile`
  - 是否已经升格为通用规则
  - 验证方式与结果
- 若某个问题没有升格为通用规则，也要记录为什么没有升格，例如：
  - 仍需更多样本证明
  - 只是 profile 缺失
  - 只是单文档数据脏点
- 不删除历史问题，只更新其状态：`已修 / 部分缓解 / 未修 / 误报`。

## 批次基线

- 当前 SERES 主批次：`seres_mda_customer_docs`
- 当前完整基线：`prototype/eval/extraction_batches/seres_mda_20260424_fix25`
- 当前 legacy no-regression 对照：`prototype/eval/extraction_batches/legacy_benchmark_core_20260424_fix25`
- 当前最新 focused residual：`prototype/eval/extraction_diagnose/j02_311_20260424_fix25`
- 当前仓库只保留 batch / diagnose 摘要与规则结论；runtime 导出报告、截图、source snapshot 仍作为本地运行证据，不再进入版本库。
- fix25 SERES 基线结果：
  - structural quality：`99 / 102`，`97.06%`，`PASS`
  - fidelity：`72 / 72`，`100%`，`PASS`
  - regression-suite：`PASS`
  - batch-report：`PASS`
  - oil-pump-benchmark：`FAIL`
- SERES batch diagnose 摘要：
  - `docs = 12`
  - `total_findings = 20`
  - `pattern_candidate_count = 20`
  - `finding_type_counts = {parser_low_confidence_hotspot: 11, profile_unmatched: 9}`
  - `suspected_layer_counts = {parser: 11, gate: 9}`
- legacy fix25 对照结果：
  - 与 `legacy_benchmark_core_20260423_fix24` 同量级：`quality = 38 / 40`、`95%`，`fidelity = 80 / 96`、`83.33%`
  - `oil-pump-benchmark` 仍是 `FAIL`，但 `产品设计规格说明书PDS-DE前后油泵总成-V1.1-20251215.pdf` 维持 `quality = 100%`、`fidelity = 95.83%`，未回退
- fix25 focused residual：
  - `docs = 1`
  - `total_findings = 1`
  - `pattern_candidate_count = 1`
  - `docs_requiring_human_review = 1`
  - `finding_type_counts = {parser_low_confidence_hotspot: 1}`
  - `suspected_layer_counts = {parser: 1}`

## 文档清单与结构画像

| 文档 | requirement 数 | profile | 结构画像 | 当前状态 |
| --- | ---: | --- | --- | --- |
| `AES-CMAC128算法伪代码20250410【AAA】.pdf` | 11 | 无 | 短算法参考，章节少，伪代码/步骤型内容为主 | 已纳入批次；暂无已升格规则 |
| `Q-SKJ00.14-2025_汽车电子控制单元版本管理编码规则【机密】.pdf` | 53 | 无 | 规则编号、版本编码、章节引用混杂，容易误触发 local clause | 已修 version/reference fake clause |
| `Q-SK J02.226-2023_CAN(FD) AUTOSAR直接网络管理需求规范.pdf` | 19 | 无 | 规范型编号文档，层级中等，图表少 | 已纳入批次；暂无已升格规则 |
| `Q-SK J02.233-2023_CANFD 通信需求规范(2MbpS) 20231024.pdf` | 55 | 无 | 通信需求规范，表格/局部编号较多 | 已确认 duplicate code finding 属噪音，不作真缺陷处理 |
| `Q-SK J02.311-2025_UDS on IP、CAN、CANFD诊断技术规范.pdf` | 333 | `seres_q_sk_j02_311` | 长文档，目录页、深层列表项、章节引用较多，且显式图/表标题存在跨页、续表、标题在图下方等裁切难点 | 已修目录污染、伪 clause、伪 section、preview owner drift、显式视觉裁切与 tolerated drift 诊断噪音 |
| `Q-SK J02.321-2025_CAN、CANFD、Ethernet、LIN BootLoader刷写规范.pdf` | 152 | 无 | 大型刷写规范，表格/局部编号较多 | 已确认 duplicate code finding 属噪音，不作真缺陷处理 |
| `Q-SK J02.385-2024_总线网络E2E通信保护设计要求 (1).pdf` | 92 | 无 | 标准规范，含 `Profile 1A` 这类英文型子标题 | 已修 profile heading 吞节问题 |
| `Q-SK J02.71-2023_CAN(FD)-CAN(FD)网关需求规范.pdf` | 56 | 无 | 网关需求规范，典型编号结构 | 已纳入批次；暂无已升格规则 |
| `Q-SK J02.86-2024_CAN(FD)间接网络管理需求规范.pdf` | 17 | 无 | 短规范，章节层级较浅 | 已纳入批次；暂无已升格规则 |
| `【机密】V32&K89_CTS_ASC_V1.1.0_20250630 (1).pdf` | 43 | `seres_v32_k89_cts_asc` | 图文混排、表格密集、截图密集、跨页 section 边界复杂 | 本系列最重要问题样本，已落地多条通用规则 |
| `赛力斯UDS刷写规范 安全签名算法要求V1.1_20250410【AAA】.pdf` | 16 | `seres_uds_signing_v1_1` | 短安全算法补充文档，结构相对稳定 | 已纳入 gate/profile，当前稳定 |
| `赛力斯UDS诊断规范 27服务安全算法要求V2.9_20250410【AAA】.pdf` | 18 | 无 | 短安全算法补充文档，章节较浅 | 已纳入批次；暂无已升格规则 |

## 系列共性问题画像

- 章节标题与表格/截图强耦合：
  - 标题可能位于表页顶部、截图附近或图表之间，parser 和 extractor 都容易错过。
- 编号噪音高：
  - `SW:AC.1.16.23`、`见 7.2 章节`、`1）/2）/3）`、`Profile 1A` 这类内容同时像“结构编号”和“正文内容”，极易触发误判。
- 目录页污染风险高：
  - 长规范文档会把目录页混进 line entries，导致 outline、quality gate、requirement 计数一起偏移。
- 图片/表格资产归属容易错：
  - section screenshot、table snapshot、source snapshot 三套资产混用时，容易出现错挂、重复挂、邻节串图。
- gate 与真实 preview 不天然等价：
  - SERES 这批文档里，`quality/fidelity` 全绿不代表 preview 已经对齐原 PDF；profile coverage 和 review 方法必须一起收紧。

## 已升格的通用规则

| 规则 ID | 问题现象 | 根因层 | 修复方案 | 已验证文档 | 效果结论 |
| --- | --- | --- | --- | --- | --- |
| `SERES-R001` | 外链 PDF 的 table/source snapshot 写入失败，preview 只能挂白底占位或缺图 | `preview` | snapshot root 优先落到 staged PDF 同目录，不可写时回退到 workspace storage | `CTS_ASC`，整批 SERES 导出 | 外链/下载目录文档可稳定生成真实截图资产 |
| `SERES-R002` | `1）/2）/3）` 被物化成 synthetic section / heading | `preview + extractor` | simple scoped ordered item 仅保留 requirement 标签，不升格成 standalone section | `J02.311`，回归覆盖 `CTS_ASC` | `8.10.4` 清理干净，且未误伤 CTS 真结构 |
| `SERES-R003` | 版本号、章节引用被拆成 fake local clause | `extractor` | 对 version / reference pattern 加保护，不再把普通示例值和章节引用当成 clause 起点 | `J00.14`，`J02.311` | fake clause 消失，原句保留在正文/requirement 内 |
| `SERES-R004` | `Profile 1A` 被当成 measurement-title 过滤，导致真 heading 丢失 | `extractor` | 为英文 profile 型标题放宽过滤，允许保留为真 heading | `J02.385` | `5.2 Profile 1A` 已恢复 |
| `SERES-R005` | 目录页进入 section outline，导致计数和结构错乱 | `extractor` | `_build_line_entries()` 跳过目录/目次 segment | `J02.311` | section/requirement 计数回到真实值 |
| `SERES-R006` | source-faithful duplicate code 被误报为真结构错误 | `gate` | duplicate code finding 不再直接作为结构缺陷进入 quality gate / diagnose 结论 | `J02.233`，`J02.321` | 表格/图示局部编号不再制造假红点 |
| `SERES-R007` | 表页标题缺失，章节跳转被误拒，后续章节被吞并到上一节 | `extractor` | 强化表页 heading 恢复，并允许符合父节上下文的章节跳转 | `CTS_ASC` | `1.1 / 1.2 / 1.3 / 2 / 6 / 7 / 8` 已恢复 |
| `SERES-R008` | 图文混排页选错 parser engine，真实 heading/正文没进 page text | `parser` | 页级 parser 选型更偏向保留 heading/正文覆盖的结果，而不是只看局部文本量 | `CTS_ASC` | `4.1.2.2` 恢复，不再整段并入 `4.1.2.1` |
| `SERES-R009` | 短规范句被清洗掉，例如单句环境约束 | `extractor + preview` | 对 requirement-bearing section 内的短正文放宽清洗 | `CTS_ASC` | `5.4` 中 `环境温度≤110℃。` 已恢复 |
| `SERES-R010` | section screenshot 跨节串图、重复挂图、与正文衔接差 | `preview` | 按 section window 重裁 screenshot；不该挂图的 section 不再强挂 | `CTS_ASC` | `1.3 / 2.1 / 3.2 / 4.1.1 / 4.2.2.3 / 4.3.2 / 9.1 / 9.3` 已按 section 边界重挂 |
| `SERES-R011` | 邻近 section 的坏 table ref 被误插入当前节 | `preview` | 多 table ref 时先剔除明显坏 ref；必要时回退真实 source snapshot | `CTS_ASC` | `4.4.2` 不再插入 `4.4.1` 的错误表格截图 |
| `SERES-R012` | 原文无表头的 local-clause 二列表被强行拼装成假表头（如 `ASC_R / 说明`） | `preview + exporter + frontend` | 引入 `headerless structured table`；对已存在的 synthetic local-clause header ref 也在 preview 响应阶段剥离表头 | `CTS_ASC` | 已在 `4.4.2` live preview 复验；当前不再生成假表头 |
| `SERES-R013` | 跨页、无显式表头的左标签/右正文 layout table 只能退化成 `snapshot_only`，无法结构化 | `preview` | 基于 section-window 的 PDF line specs 恢复 `headerless structured table`；仅在标签模式、列对齐、页内边界足够稳定时触发，失败时保持原 fallback | `CTS_ASC` | `4.1.2.2` 已在 live preview 恢复为 `ASC_F / ASC_R` 两张无表头结构表 |
| `SERES-R014` | rebuild 后的 structured table 虽然有表格，但前端没有 `原文截图` 切换；同时旧坏 `table_ref` 会挡住更完整的 line-recovery 结果 | `preview + frontend` | 让所有 structured-table rebuild 路径都传递 `source_snapshot_path / source_image_block_count`；并把 line-spec recovery 提前到 pipe-table repair 之前，允许它覆盖旧的坏 `table_ref` | `CTS_ASC` | `4.1.2.2 / 4.4.2` 当前都已在 live preview 出现 `表格 / 原文截图`；`4.4.2` 同时补回了 `ASC_F 前置条件` 与 `ASC_R 备注` |
| `SERES-R015` | 多 group 的 layout-table rebuild 共用同一张 section-window source snapshot，导致 `ASC_F / ASC_R` 的 `原文截图` 完全相同；同时跨 subject 的尾 `备注` 会被吞进最后一张表 | `preview` | 为 recovered group 保存 `row_specs / used_indexes`，按 group 单独构建 crop plan 与 source snapshot；再把跨 subject 的尾 `备注` 从最后一张表里剥离为 section-level note block | `CTS_ASC` | `4.1.2.2 / 4.4.2` 当前都已在 live preview 显示不同的 `ASC_F / ASC_R` source snapshot，且 `4.4.2` 的 `备注` 已移出表格 |
| `SERES-R016` | simple scoped ordered item 虽然不再升成 section，但 review preview 里的 owner 仍漂到 `1）/2）/3）` 自身，导致 section 列和过滤归属错误 | `preview` | 在 `_attach_requirement_section_context()` 中，当 scoped local clause 不应 surface 成 section 时，把 `owner_section_* / parent_section_*` 回退到 `structural_parent_*`，而不是继续保留 clause 自身 owner | `J02.311` | `8.13.4 / 8.14.4 / 8.18.5 / 8.20.4` 当前已稳定显示在父 section 下；fix25 focused diagnose 不再把 tolerated drift 误报成 complex-table residual |
| `SERES-R017` | 显式图表裁切只会从标题向下取图，或把 split note / 页眉页脚误当截止边界，导致“图标题在图下方时整图空白”“续表被截浅”“末表吞进后文” | `preview` | 显式视觉裁切改成同时评估标题上方/下方候选区，并按视觉信号与高度择优；table note continuation 不再提前截断；忽略运行页眉页脚；若同页末表没有后续显式标题，则按已观测到的最后表行收口 | `J02.311` | `7.5.5 表12（续）`、`7.5.6.1 图3/表14`、`7.5.6.2 图4/图5` 当前都已恢复成正确 source snapshot，不再出现空白图或吞后文 |
| `SERES-R018` | fidelity profile 已允许的小幅 requirement drift 被诊断层误当成复杂表 clause binding 漂移 | `diagnose / gate` | baseline delta 记录 profile 允许的 requirement/section drift 范围；只有漂移超出范围、或存在 table row / duplicate scope 证据时才生成 `complex_table_clause_binding_drift` | `J02.311` | `requirements_drift = -1` 已按 tolerated drift 处理；focused diagnose 从 `2` 个 residual 降到 `1` 个，仅剩 `parser_low_confidence_hotspot` |

## CTS_ASC 专项问题台账

`CTS_ASC` 是当前 SERES 系列中最典型、最值钱的规则样本。下面记录这轮已经确认过的真实问题、处理方式和当前效果。

| 原始问题 | 根因层 | 修复方案 | 当前效果 |
| --- | --- | --- | --- |
| `1.1 / 1.2 / 1.3` 缺失，但 gate 仍全绿 | `extractor + gate` | 恢复表页 heading；把 CTS 关键 section / visual asset 编进 profile，不再只看泛化 gate | 用户已在真实前端确认 `1.1 / 1.2 / 1.3` 全部正确；也解释了此前“绿灯漏检”的根因 |
| `2 功能分配` 丢失并被并入 `1.6.3` | `extractor` | 修正章节跳转误拒逻辑 | 用户已在真实前端确认 `2` 恢复为独立 section |
| `2.1 承接功能清单` 四行表格被识别成三行，截图丢第四行 | `preview` | 用真实 source snapshot 替换白底占位；修复 snapshot root fallback | 用户已在真实前端确认当前展示正确；首轮截图因裁切问题低估了效果 |
| `4.1.2.1` 与 `4.1.2.2` 混并，`4.1.2.2 功能策略` 被吞；恢复出表格后最初没有 `原文截图` 切换，随后又出现 `ASC_F / ASC_R` 共用同一张 source 图 | `parser + extractor + preview` | 先修正图文混排页 parser 选型并恢复真 heading，再用 section-window line-spec recovery 把稳定的无表头 layout table 升成结构表；随后补齐 rebuilt ref 的 `source_snapshot_path` 传递；最后按 group 单独裁切 source snapshot，避免双表共用一张图 | 本轮 `5555` live preview 已能看到 `ASC_F / ASC_R` 两张无表头结构表，且两张 `原文截图` 已不同；用户已确认当前效果符合预期 |
| `4.2.2.3 功能框图` 的图片未识别 | `preview` | 用 section-window 真实 screenshot/source snapshot 补挂 | 第二批前端证据 `cts_4_2_2_3_refresh.png` 已能看到图像回挂，用户已确认正确 |
| `4.2.2.4 高压互锁检测流程` 文字与截图衔接差，截图重复且大 | `preview` | 多张重叠 structured screenshot 收敛为 `1` 张 section screenshot | 第二批前端证据 `cts_4_2_2_4_refresh.png` 显示当前为 `1` 张推荐截图 + `1` 张 source snapshot；现状优于之前，但图文衔接仍有继续细化空间 |
| `4.4.2` 误插入 `4.4.1` 表格截图；结构表头看起来像拼装；且曾缺少 page 21 末 `ASC_F 前置条件`、文末 `备注` 与 `原文截图` 切换；之后又发现 `ASC_F / ASC_R` 共用同一张 source 图，且 `备注` 被吞进最后一张表 | `preview + frontend` | 先剔除坏 table ref；再把 local-clause 二列表改成 `headerless structured table`，并对历史 synthetic header ref 做剥离；随后让 line-spec recovery 覆盖旧坏 ref，并把 rebuilt ref 的 source snapshot 元信息传到前端；最后按 group 单独裁切 source snapshot，并把跨 subject 的尾 `备注` 提升为 section-level note block | 错挂 `4.4.1` 的症状已消失；本轮 `5555` live preview 已复验假表头消失、`ASC_F 前置条件` 与 `ASC_R 备注` 回到 preview，`ASC_F / ASC_R` 的 `原文截图` 已不同，且 `备注` 已移出表格；用户已确认当前效果符合预期 |
| `5.4 环境要求` 中 `环境温度≤110℃。` 丢失 | `extractor + preview` | 保留短规范句 | 第二批前端证据 `cts_5_4_refresh.png` 已看到 `环境温度≤110℃。` |
| `6 / 7 / 8` 被吞进 `5.6.3` | `extractor` | 强化 heading 恢复与章节跳转 | 第二批前端证据 `cts_6_fullpage_seq.png`、`cts_7_refresh.png`、`cts_8_refresh.png` 已看到 heading/正文回到 preview |
| `9.1 / 9.3` 图片未识别和插入 | `preview` | source snapshot / section screenshot 回退到真实 PDF 原图 | 第二批前端证据 `cts_9_1_refresh.png`、`cts_9_3_fullpage_seq.png` 已看到图片回挂；`9.3` 仍未做逐图 caption pairing |

## CTS_ASC 前端截图验收记录（2026-04-17 持续更新）

| 项目 | 证据 | 当前判断 | 用户状态 |
| --- | --- | --- | --- |
| `1.1 版本历史` | [cts_1_1.png](/Users/liuchang/Documents/ReqPilotX/worktrees/_active/extraction-quality-loop/output/playwright/seres-evidence/cts_batch1/cts_1_1.png) | 表页 heading 与表格已恢复 | 用户已确认正确 |
| `1.2 缩写` | [cts_1_2.png](/Users/liuchang/Documents/ReqPilotX/worktrees/_active/extraction-quality-loop/output/playwright/seres-evidence/cts_batch1/cts_1_2.png) | 表页 heading 与表格已恢复 | 用户已确认正确 |
| `1.3 图例` | [cts_1_3.png](/Users/liuchang/Documents/ReqPilotX/worktrees/_active/extraction-quality-loop/output/playwright/seres-evidence/cts_batch1/cts_1_3.png) | section 与原文截图已恢复 | 用户已确认正确 |
| `2 功能分配` | [cts_2_heading.png](/Users/liuchang/Documents/ReqPilotX/worktrees/_active/extraction-quality-loop/output/playwright/seres-evidence/cts_batch2/cts_2_heading.png) | 顶层 heading 已独立恢复 | 用户已确认正确 |
| `2.1 承接功能清单` | [cts_2_1.png](/Users/liuchang/Documents/ReqPilotX/worktrees/_active/extraction-quality-loop/output/playwright/seres-evidence/cts_batch2/cts_2_1.png) | source snapshot 已回挂，当前 preview 已恢复正确展示 | 用户已确认正确 |
| `4.1.2.2 功能策略` | [cts_4_1_2_2_viewport_verify.png](/Users/liuchang/Documents/ReqPilotX/worktrees/_active/extraction-quality-loop/output/playwright/seres-evidence/cts_batch2/cts_4_1_2_2_viewport_verify.png)、[cts_4_1_2_2_source_toggle_live.png](/Users/liuchang/Documents/ReqPilotX/worktrees/_active/extraction-quality-loop/output/playwright/seres-evidence/cts_batch2/cts_4_1_2_2_source_toggle_live.png)、[cts_4_1_2_2_source_split_verify.png](/Users/liuchang/Documents/ReqPilotX/worktrees/_active/extraction-quality-loop/output/playwright/seres-evidence/cts_batch2/live_verify/cts_4_1_2_2_source_split_verify.png)、[cts_4_1_2_2_asc_f_source_only.png](/Users/liuchang/Documents/ReqPilotX/worktrees/_active/extraction-quality-loop/output/playwright/seres-evidence/cts_batch2/live_verify/cts_4_1_2_2_asc_f_source_only.png)、[cts_4_1_2_2_asc_r_source_only.png](/Users/liuchang/Documents/ReqPilotX/worktrees/_active/extraction-quality-loop/output/playwright/seres-evidence/cts_batch2/live_verify/cts_4_1_2_2_asc_r_source_only.png) | 本轮 `5555` live preview 已恢复为 `ASC_F / ASC_R` 两张无表头结构表；并已出现 `表格 / 原文截图` 双切换，且 `ASC_F / ASC_R` 当前已显示不同的原文图，不再共用同一张 source snapshot | 用户已确认符合预期 |
| `4.2.2.3 功能框图` | [cts_4_2_2_3_refresh.png](/Users/liuchang/Documents/ReqPilotX/worktrees/_active/extraction-quality-loop/output/playwright/seres-evidence/cts_batch2/cts_4_2_2_3_refresh.png) | 图片已回挂 | 用户已确认正确 |
| `4.2.2.4 高压互锁检测流程` | [cts_4_2_2_4_refresh.png](/Users/liuchang/Documents/ReqPilotX/worktrees/_active/extraction-quality-loop/output/playwright/seres-evidence/cts_batch2/cts_4_2_2_4_refresh.png) | 当前 preview 已不再是首轮的重复大图；图文衔接仍可继续细化 | 用户已确认 `ok` |
| `4.4.2 ASC_R` | [cts_4_4_2_viewport_verify.png](/Users/liuchang/Documents/ReqPilotX/worktrees/_active/extraction-quality-loop/output/playwright/seres-evidence/cts_batch2/cts_4_4_2_viewport_verify.png)、[cts_4_4_2_source_toggle_live.png](/Users/liuchang/Documents/ReqPilotX/worktrees/_active/extraction-quality-loop/output/playwright/seres-evidence/cts_batch2/cts_4_4_2_source_toggle_live.png)、[cts_4_4_2_slice_verify.png](/Users/liuchang/Documents/ReqPilotX/worktrees/_active/extraction-quality-loop/output/playwright/seres-evidence/cts_batch2/cts_4_4_2_slice_verify.png)、[cts_4_4_2_source_split_verify.png](/Users/liuchang/Documents/ReqPilotX/worktrees/_active/extraction-quality-loop/output/playwright/seres-evidence/cts_batch2/live_verify/cts_4_4_2_source_split_verify.png)、[cts_4_4_2_asc_f_source_only.png](/Users/liuchang/Documents/ReqPilotX/worktrees/_active/extraction-quality-loop/output/playwright/seres-evidence/cts_batch2/live_verify/cts_4_4_2_asc_f_source_only.png)、[cts_4_4_2_asc_r_source_only.png](/Users/liuchang/Documents/ReqPilotX/worktrees/_active/extraction-quality-loop/output/playwright/seres-evidence/cts_batch2/live_verify/cts_4_4_2_asc_r_source_only.png)、[cts_4_4_2_note_block_only.png](/Users/liuchang/Documents/ReqPilotX/worktrees/_active/extraction-quality-loop/output/playwright/seres-evidence/cts_batch2/live_verify/cts_4_4_2_note_block_only.png) | 本轮 `5555` live preview 已确认无假表头、无 `4.4.1` 错图；`ASC_F / ASC_R` 当前已显示不同的原文图；`备注` 已独立显示为 section note block，不再留在 `ASC_R` 表里；complex-table binding residual 仅剩机器侧信号 | 用户已确认符合预期 |
| `5.4 环境要求` | [cts_5_4_refresh.png](/Users/liuchang/Documents/ReqPilotX/worktrees/_active/extraction-quality-loop/output/playwright/seres-evidence/cts_batch2/cts_5_4_refresh.png) | `环境温度≤110℃。` 已恢复 | 用户已确认正确 |
| `6 法规需求` | [cts_6_fullpage_seq.png](/Users/liuchang/Documents/ReqPilotX/worktrees/_active/extraction-quality-loop/output/playwright/seres-evidence/cts_batch2/cts_6_fullpage_seq.png) | heading 已恢复为独立 section | 用户已确认正确 |
| `7 功能安全需求` | [cts_7_refresh.png](/Users/liuchang/Documents/ReqPilotX/worktrees/_active/extraction-quality-loop/output/playwright/seres-evidence/cts_batch2/cts_7_refresh.png) | heading 与正文已恢复 | 用户已确认正确 |
| `8 信息安全需求` | [cts_8_refresh.png](/Users/liuchang/Documents/ReqPilotX/worktrees/_active/extraction-quality-loop/output/playwright/seres-evidence/cts_batch2/cts_8_refresh.png) | heading 与正文已恢复 | 用户已确认正确 |
| `9.1 产品效果图` | [cts_9_1_refresh.png](/Users/liuchang/Documents/ReqPilotX/worktrees/_active/extraction-quality-loop/output/playwright/seres-evidence/cts_batch2/cts_9_1_refresh.png) | 图片已回挂 | 用户已确认正确 |
| `9.3 安装位置` | [cts_9_3_fullpage_seq.png](/Users/liuchang/Documents/ReqPilotX/worktrees/_active/extraction-quality-loop/output/playwright/seres-evidence/cts_batch2/cts_9_3_fullpage_seq.png) | 双图已回挂；仍未做逐图 caption pairing | 用户已确认“效果已经很好了，完美” |

## 其它 SERES 文档问题台账

| 文档 | 发现的问题 | 处理方案 | 有效性 |
| --- | --- | --- | --- |
| `Q-SKJ00.14` | `SW:AC.1.16.23 / HW:C.1.3` 被拆成伪 local clause；`4.1.5 / 5.1` 出现 fake requirement 行 | `SERES-R003` version/reference fake clause guard | 真实核对已确认消失 |
| `Q-SK J02.311` | `7.6.3.2` 的 `见 7.2 章节` 被拆成伪 clause；`8.10.4` 下 `1）/2）/3）` 长成伪 section；目录页污染 outline；`8.13.4 / 8.14.4 / 8.18.5 / 8.20.4` 的 requirement owner 在 review preview 中仍漂到 `1）/2）/3）` 自身；`7.5.5 / 7.5.6.1 / 7.5.6.2` 的显式图表截图存在续表截浅、图标题在下方导致空白图、末表吞入后文；诊断层曾把 profile 容许的 `requirements_drift = -1` 误报成 complex-table residual；`2026-04-24` 用户反馈确认仍有 nested list 丢层级、图/表截图不准、附录 A 开头内容缺失等未闭环问题 | `SERES-R002`、`SERES-R003`、`SERES-R005`、`SERES-R016`、`SERES-R017`、`SERES-R018`，另见“J02.311 用户反馈未闭环问题台账” | `sections_count = 344`，部分伪 section / owner drift / diagnose 噪音已缓解；但不能再标为“J02.311 已收口”。当前有效结论：fix25 只关闭了 tolerated drift 误报，用户可见 extraction / screenshot / appendix 问题仍是 `OPEN` |
| `Q-SK J02.385` | `5.2 Profile 1A` 被吞进 `5.1` | `SERES-R004` | 真 heading 已恢复 |
| `Q-SK J02.233` | `5.4` 等编号看起来像重复结构，但实际是表格/图示局部编号 | `SERES-R006` gate noise suppression | 已确认属于诊断噪音，不再按真结构缺陷追 |
| `Q-SK J02.321` | `4 / 5 / 6 / 7.3` 等局部编号曾被误看成结构缺陷 | `SERES-R006` gate noise suppression | 已确认属于诊断噪音，不再按真结构缺陷追 |
| `AES-CMAC` / `J02.226` / `J02.71` / `J02.86` / `UDS 27` | 当前主要是 `parser_low_confidence_hotspot` 或 `profile_unmatched`，还没有形成可靠通用规则 | 暂不硬升规则，继续等更多样本 | 已记录为待观察，不伪装成“已修” |
| `UDS signing v1.1` | 当前无明确新问题，主要作用是作为已建 profile 的稳定 gate 样本 | 维持 profile/gate 基线 | 当前稳定 |

## J02.311 低置信页人工核对记录

| 页码 | 当前判断 | 备注 |
| --- | --- | --- |
| `1` | parser 风险，无当前可见回归 | 首页发布信息页，当前未见 section / requirement 可见漂移 |
| `11` | parser 风险，无当前可见回归 | `术语/缩略语` 区域；当前 preview 可见内容稳定 |
| `12` | parser 风险，无当前可见回归 | 当前 review entries 未见可见 requirement 漂移 |
| `13` | parser 风险，无当前可见回归 | 当前 review entries 未见可见 requirement 漂移 |
| `70` | parser 风险，无当前可见回归 | `8.9.4 / 8.10` 交界页；当前节归属未见异常 |
| `89` | parser 风险，无当前可见回归 | `8.11.4 参数定义` 表页；当前 preview 未见错挂/丢行 |
| `105` | parser 风险，无当前可见回归 | `8.17.4 参数定义` 表页；当前 preview 未见错挂/丢行 |
| `108` | parser 风险，无当前可见回归 | `8.18.4 参数定义` 表页；当前 preview 未见错挂/丢行 |
| `112` | parser 风险，无当前可见回归 | `8.19.4 参数定义` 表页；当前 preview 未见错挂/丢行 |

这一轮结论是：`J02.311` 的低置信页当前仍先按 parser 风险记录；但可见问题不再只有 owner drift，还包括一组已经修完的显式图/表裁切问题。修完后，这 9 个低置信页里仍没有新增肉眼可见的 preview 回退。

## J02.311 图表与截图问题台账（2026-04-18）

| 原始问题 | 根因层 | 处理方案 | 证据 | 当前状态 |
| --- | --- | --- | --- | --- |
| `6.2.2 故障自诊断范围` 中 `d）` 向下的 `1）~6）` 与 `e）~h）` 用户反馈看不到；后续复核发现数字项虽存在，但从 `d）` 下方层级漂成独立数字行 | `preview / extractor` | 先回到 live review 与 preview JSON 双重核对；后续修复应保留 nested list parent-child 关系，而不是只证明文字存在 | live review 复核 + preview JSON requirement 行 | `部分缓解 / OPEN`：文字存在，但 nested 层级不正确 |
| `6.3.2.2` 的 `表3 / 表4 / 表5` 截图无效、截多 | `preview` | 复核显式 table crop 的截止边界，避免邻近表/正文串入；这一轮复核时已确认当前截图正确，无需再次改代码 | live review 复核 | 当前判定为“已恢复，截图有效” |
| `7.2 / 7.3 表10` 没有识别成结构表，但文字和节归属基本正确 | `preview` | `2026-04-23 fix23` 明确接受 `snapshot_only` fallback；要求 `7.2` 保留原文截图且不能吞掉 `7.3` heading / requirement；在没有稳定 line specs / geometry 证据前不强推结构化 | live review + preview JSON + focused test 复核 | `accepted fallback`：`7.2` 保持 `snapshot_only`，`7.3` 独立稳定；当前不升格新规则 |
| `7.5.5 表12 续表` 识别错误、截图错误；后续用户反馈确认截图准确、重复问题已解决，但 Markdown 表格渲染仍不行 | `preview / frontend export` | `SERES-R017` 已缓解截图边界；后续要单独处理 `snapshot_only` 与 markdown/structured render 的展示一致性 | [表12 续表 source snapshot](/Users/liuchang/Documents/ReqPilotX/worktrees/_active/extraction-quality-loop/prototype/storage/uploads/table_snapshots/src_474f9ba4f38cd7db.png)、[7.5.5 live DOM](/Users/liuchang/Documents/ReqPilotX/worktrees/_active/extraction-quality-loop/output/playwright/seres-evidence/j02311_live_verify/.playwright-cli/page-2026-04-18T08-42-00-047Z.yml) | `部分缓解 / OPEN`：截图已修，MD 表格渲染未闭环 |
| `7.5.6.1 图3 / 表13 / 表14` 截图与识别不稳定；用户反馈 `表14` 没识别到或截图落下 | `preview` | `SERES-R017` 只证明部分 source snapshot 能产出；后续必须按前端可见状态逐图/逐表核对，而不是只看 table_ref 存在 | [图3 source snapshot](/Users/liuchang/Documents/ReqPilotX/worktrees/_active/extraction-quality-loop/prototype/storage/uploads/table_snapshots/src_d65c251746aca81c.png)、[表14 source snapshot](/Users/liuchang/Documents/ReqPilotX/worktrees/_active/extraction-quality-loop/prototype/storage/uploads/table_snapshots/src_9829b514d4e47b54.png)、当前 live `table_refs` 中 `表14` 存在但用户可见验收未通过 | `OPEN`：不能再标为已修 |
| `7.5.6.2 图4 / 图5` 没识别到、无截图 | `preview` | `SERES-R017`：figure crop 允许标题在图下方，并按视觉信号选图区 | [图4 source snapshot](/Users/liuchang/Documents/ReqPilotX/worktrees/_active/extraction-quality-loop/prototype/storage/uploads/table_snapshots/src_9594fc8e3ad85a23.png)、[图5 source snapshot](/Users/liuchang/Documents/ReqPilotX/worktrees/_active/extraction-quality-loop/prototype/storage/uploads/table_snapshots/src_a923352776805594.png) | `已修` |

## J02.311 用户反馈未闭环问题台账（2026-04-24）

这一节记录用户直接指出、但此前未进入长台账或被过早标为“已修”的问题。当前状态统一按 `OPEN` 处理，直到 live preview + PDF 原页 + source snapshot 三方验收通过。

| 位置 | 用户反馈 / 当前 live 证据 | 初步归因 | 当前状态 / 下一步 |
| --- | --- | --- | --- |
| `1 范围` / 用户称 `1.1` | `a）/b）/c）` 已并入主 requirement，但属于 `b）` 的 `1）/2）` 被拆成独立数字行，失去 `b）` 子项关系；`图1` source snapshot 当前只截到 caption 和页码，真实图内容被 OCR 文本混进 requirement 文本 | `preview nested-list repair + figure crop` | `OPEN`：先修 nested alpha/numeric hierarchy，再修 caption-below figure crop；当前证据路径：`src_556ecffc51b9628e.png` 只含 `图1` 标题 |
| `6.2.2` | `d）` 下的 `1）~6）` 文字存在，但作为独立数字行漂出 `d）` 层级；此前“可见即已修”的判断不充分 | `preview / extractor hierarchy` | `OPEN`：需要保持 `d） -> 1）~6） -> e）~h）` 顺序和嵌套关系 |
| `6.6.1` | 两组 `1）/2）...` 分别应归属 `a）` 与 `b）`，当前 live preview 中数字行扁平排列，容易丢失父 alpha 语义 | `preview / extractor hierarchy` | `OPEN`：作为同一类 nested list 修复的回归样本 |
| `5.1.4` / 当前 live 定位疑似 `5.14` | 用户反馈“没有表格却误识别表格并截图”；当前 live 中 `5.14 诊断报文优先级` 确有 structured table ref，source page 指到 page 18，明显与原 section/page 不一致 | `table detector false positive + owner drift` | `OPEN`：需确认用户所指是否为 `5.14`，并增加无表格段落不得强造表格的 guard |
| `7.5.2 表11` | 用户反馈表 11 截图不全，跨页部分未加入；当前 live 有两个 refs，但仍需逐页核对是否覆盖完整表体 | `explicit table continuation crop` | `OPEN`：需要按原 PDF 表11 页边界重裁，并补跨页 continuation 识别 |
| `7.5.5 表12` | 用户确认截图准确、重复表格问题已解决，但 MD 表格渲染不行 | `frontend/export render` | `OPEN`：截图问题降级为已缓解；展示/导出渲染仍需修 |
| `7.5.6.1 表14` | 用户反馈表 14 没识别到、截图落下；当前 live 有 `表14` table_ref，但用户可见验收未通过 | `preview visibility / crop / table asset binding` | `OPEN`：不能仅凭 table_ref 存在判定已修，需前端视图复核 |
| `7.6.1.1` | 用户反馈截图和内容很乱；当前 live `图6/图7` 有多个 snapshot refs，需要核对是否错切/重复/串正文 | `figure multi-caption crop` | `OPEN`：纳入图组裁切专项 |
| `7.6.2.1 / 7.6.2.2 / 7.6.2.3` | 用户反馈 `图8/图9/图10` 没截对，识别到大量文字；已抽查 `图9` source snapshot，确实包含下一节正文而不是架构图主体 | `caption-below / previous-title boundary / text-heavy crop` | `OPEN`：优先修显式 figure crop 选图逻辑，避免把正文当图 |
| `附录 A / 表 A.1` | 用户反馈 135 页附录 A 开头内容全丢；当前 live 有 `附录 A` heading，但表 A.1 引言、`00 positiveResponse` 开头没有正确出现，预览从 `01-0F` 错误位置开始 | `appendix parser/extractor + table start boundary` | `OPEN`：需要恢复 appendix root prose 和表 A.1 第一行，不能只生成 `Appendix A` heading |

## 为什么上一次 review preview 没有及时发现

- 当时 review 方法不够严：
  - 过度信任了 `quality/fidelity` 绿灯和摘要数字，没有逐 section 对照 PDF 原页、preview、table/source snapshot 三方。
- 当时 profile coverage 不够：
  - CTS 这类关键缺节、缺图、错图问题当时没有全部编进 gate/profile，导致“结构绿了，但 preview 还是错的”。
- 这次已经形成明确规则：
  - SERES 系列必须按 section checklist 做人工对照。
  - 发现问题后必须回写到本台账，而不是只留在临时诊断产物里。

## 规则有效性证据

- 聚焦回归：
  - `python3 -m unittest prototype.tests.test_extraction_diagnose -q` -> `8 tests / OK`
  - `python3 -m unittest prototype.tests.test_extraction_preview -q` -> `52 tests / OK`
  - `python3 -m unittest prototype.tests.test_extractor_sections -q` -> `69 tests / OK`
- 真实文档核对：
  - `J00.14`：version example fake clause 已消失
  - `J02.311`：`8.10.4` 无 synthetic child section / heading
  - `J02.311`：`8.13.4 / 8.14.4 / 8.18.5 / 8.20.4` 的 requirement row 当前已在 review preview 稳定归属到父 section
  - `J02.311`：`6.2.2` 当前可见 `d）1）~6）` 及 `e）~h）`，但 `1）~6）` 仍有从 `d）` 下方漂成独立数字行的层级问题，不能再按“已修”关闭
  - `J02.311`：`6.3.2.2` 的 `表3 / 表4 / 表5` 当前 source 截图可用，未再出现“空白图 / 串邻表 / 吞正文”
  - `J02.311`：`2026-04-23` live review 明确确认 `7.2` 的 `图2 / 表10` 继续保留 `snapshot_only` fallback，而 `7.3` 没有被上一节吞并
  - `J02.311`：`2026-04-24 fix25` focused diagnose 明确确认 tolerated `requirements_drift = -1` 不再产生 `complex_table_clause_binding_drift`
  - `J02.311`：`7.5.5` 截图重复/边界问题已部分缓解，但 MD 表格渲染仍未闭环；`7.5.6.1 表14` 用户可见验收未通过，不能再按“已修”关闭
  - `J02.385`：`5.2 Profile 1A` 已恢复
  - `CTS_ASC`：`4.1.2.2` 已在 live preview 恢复为无表头结构表；`4.4.2` 已在 live preview 复验不再生成假表头
  - `CTS_ASC`：`4.1.2.2 / 4.4.2` 当前都已在 live preview 出现 `表格 / 原文截图` 双切换
  - `CTS_ASC`：`4.4.2` 在 `tableView=slice` 视图里已可直接看到 `ASC_F 前置条件` 与 `ASC_R 备注`
  - `CTS_ASC`：`4.1.2.2 / 4.4.2` 当前都已在 live preview 显示不同的 `ASC_F / ASC_R` 原文图
  - `CTS_ASC`：`4.4.2` 的 `备注` 当前已从最后一张表内移出，成为 section 级独立 note block
  - `CTS_ASC`：`4.2.2.4 / 5.4 / 9.1 / 9.3` 在新规则下仍稳定
- 批次基线：
  - `seres_mda_20260424_fix25` 当前是这组客户文档的完整验证基线
  - `quality = 97.06% PASS`，`fidelity = 100% PASS`
  - `J02.311` 当前 focused diagnose 已切到 `j02_311_20260424_fix25`，残余只剩 `parser_low_confidence_hotspot`
  - SERES 整批 diagnosis 当前为 `20`：`parser_low_confidence_hotspot = 11`、`profile_unmatched = 9`
  - required profile 全部匹配：`seres_q_sk_j02_311`、`seres_v32_k89_cts_asc`、`seres_uds_signing_v1_1`
- 在线复核：
  - `5555` review 服务已在新代码上运行
  - `http://127.0.0.1:5555/api/health = 200`
  - `http://127.0.0.1:5555/workspace?stage=review = 200`
  - review 页面现支持 QA deep-link：
    - `projectId / documentId / section / keyword / sourceOnly / tableView`
    - 可直接固定到某份 SERES 文档、某个 section、某种表格视图做人工复核与截图
  - 本轮新增真实前端截图工件：
    - `output/playwright/seres-evidence/cts_batch1`
    - `output/playwright/seres-evidence/cts_batch2`

## 当前残余

- `CTS_ASC`
  - page `29` 仍有 `parser_low_confidence_hotspot`
  - `9.3` 仍是 section 级图片挂接，尚未做逐图 caption pairing
  - 当前已无新的用户可见显性错误；剩余更像机器侧稳健性债务
- `SERES` 整批
  - `J02.311` 当前已完成 `fix25` 复核；最新 focused diagnose `j02_311_20260424_fix25` 只保留 `parser_low_confidence_hotspot`
  - `J02.311` 当前没有 `complex_table_clause_binding_drift` residual；此前 `requirements_drift = -1` 属 profile tolerated drift。
  - `J02.311` 当前仍有用户可见 `OPEN` 问题：nested alpha/numeric list 层级丢失、`图1 / 图8 / 图9 / 图10` 等显式图 crop 错切、`5.14` 疑似 false table、`7.5.2 表11` 跨页不全、`7.5.6.1 表14` 可见缺失、`附录 A / 表A.1` 开头缺失。
  - `J02.311` 的 `7.2 / 7.3 表10` 当前按 accepted fallback 处理，不视为新的主逻辑缺口
  - `J02.311` 当前仍有 parser 风险页：`1 / 11 / 12 / 13 / 70 / 89 / 105 / 108 / 112`
  - `profile_unmatched = 9` 说明仍有 9 份 SERES 文档没有专属 profile
  - `parser_low_confidence_hotspot = 11` 说明 parser 侧仍有审计空间，但不能直接等价为用户可见回归

## 后续维护方式

- 每轮 SERES 审计结束后：
  - 先更新本文件的“问题台账”和“规则有效性证据”
  - 再只把当前状态摘要写回 active-context
- 只有满足下面条件，才把一个问题升格为仓库级通用规则：
  - 至少在两个样本上复现，或在一个高价值文档上造成系统性损伤
  - 能明确归因到 `parser / extractor / preview / gate / profile` 之一
  - 有 focused test、diagnose 或真实 PDF 对照作为证据
  - 不会引入对 oil-pump / legacy benchmark 的明显回归
- 如果只是 profile 缺失或 diagnose 噪音，不要伪装成“抽取逻辑已修复”。

## 相关工件

- [SERES batch fix25 summary](/Users/liuchang/Documents/ReqPilotX/worktrees/_active/extraction-quality-loop/prototype/eval/extraction_batches/seres_mda_20260424_fix25/batch_summary.json)
- [SERES batch fix25 diagnose summary](/Users/liuchang/Documents/ReqPilotX/worktrees/_active/extraction-quality-loop/prototype/eval/extraction_batches/seres_mda_20260424_fix25/diagnosis_summary.json)
- [J02.311 latest diagnose fix25](/Users/liuchang/Documents/ReqPilotX/worktrees/_active/extraction-quality-loop/prototype/eval/extraction_diagnose/j02_311_20260424_fix25/diagnosis_summary.json)
- [Legacy benchmark fix25 summary](/Users/liuchang/Documents/ReqPilotX/worktrees/_active/extraction-quality-loop/prototype/eval/extraction_batches/legacy_benchmark_core_20260424_fix25/batch_summary.json)
- [Extraction active context](/Users/liuchang/Documents/ReqPilotX/worktrees/_active/extraction-quality-loop/prototype/docs/extraction-active-context.md)
