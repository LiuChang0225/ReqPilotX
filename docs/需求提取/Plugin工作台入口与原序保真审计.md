# Plugin 工作台入口与原序保真审计

更新：2026-09-12。用户指出主 skill 没有引导 ReqPilotX 前端与相关脚本，也未明确按顺序高保真拆解原文。本轮按主稿、候选稿、references、CLI、原生宿主、前端及实际历史调用排查；未启动服务、模型或修改运行代码。

## 结论

问题成立。原序/保真规则和工作台能力确有既有实现，但主入口没有形成清楚的业务要求与操作交接。9月11日的短稿还删除了旧主 skill 仅有的 `workbench` 和脚本路径提示，属于简化时丢失关键信息。不能因 Agent 能复述流程就认为候选稿合格。

前端是用户核对原件、结构、条目、版本并提供反馈的主界面。**发现并打开正确文档、阶段成果可见和反馈接续属于当前 plugin 的基本闭环**；视觉、动效、布局等改版可以后置。此前计划把两类工作一起后置，应纠正。

## 前端与脚本：已有与缺失

| 环节 | 查到的事实 | 缺口 |
|---|---|---|
| 主 skill | 旧稿第55行提及 doctor/catalog/workbench；候选稿只剩泛称“展示结构”和工具索引 | 没说明何时打开哪个界面、如何发现真实项目/文档、阶段成果在哪里看、反馈如何回流；候选还丢掉具体命令入口 |
| 参考入口 | `reading-guide.md`第1步泛称打开/引导现有工作台；`workbench --open`埋在 `tools-front-matter.md` | 基础交互动作依赖 Agent 偶然读到封面/目录专项说明；`tools-host.md`也缺完整工作台上手路径 |
| 工具发现 | CLI argparse 支持 workbench，但 `catalog` 的 tools 清单未列出它 | 主稿让 Agent 经 catalog 找入口，实际目录却不暴露入口 |
| workbench 脚本 | `scripts/reqpilot.py`第193行起：检查本地根页是否含 ReqPilotX，返回 URL；`--open`调用浏览器 | 只支持 `--url`、`--open`，不会导入 review、绑定项目文档、发布阶段或处理反馈；opened 也不证明页面已展示正确成果 |
| 原生阶段发布 | `SourceDocumentLoopToolHost.invoke` 在 compile-map/items/images 成功后调用 `publish_stage`，记录任务的 stage-results | 这在原生任务宿主中执行。独立 plugin 直接调用 CLI 编译，不会自动经过该宿主 |
| 前端承接 | `DocumentWorkSurface` 与 `SourceDocumentStageResults`通过当前项目/文档/任务的 API读取 Map/Items，可查看原页、显示版本更新和确认 Map | 页面依据应用登记的任务，不会扫描任意外部 runtime-data/review 文件夹；打开首页不能替代成果接入 |
| 本地服务脚本 | `prototype/start.sh`、`stop.sh`、受限 `scripts/agent-workbench-worker.sh` 及部署说明存在 | 不是可移植插件包中的一键前端；普通 start.sh 还会处理依赖、迁移和通用 worker，不能仅为“打开页面”就不加区分地运行 |

代码入口：[workbench CLI](../../plugins/reqpilot/scripts/reqpilot.py)、[阶段发布宿主](../../prototype/app/application/agent_workflow/source_document_loop_jobs.py)、[阶段指针](../../prototype/app/application/agent_workflow/source_document_stages.py)、[前端工作区](../../prototype/frontend/src/features/workspace/DocumentWorkSurface.tsx)、[阶段读取与确认](../../prototype/frontend/src/features/extraction/SourceDocumentStageResults.tsx)。

原生任务简报已经提供 workbench_path，但 renderer 的文字强调“应用负责展示；不要声称自己已操作浏览器”。这是事实边界，不能代替给用户可用入口、指引其核对当前阶段的正向职责。前端有能力不等于独立插件已经接好。

### 已发生的具体失败

五页完整文档测试的 Sol high v2 曾执行：

```text
.venv/bin/python .reqpilot/scripts/reqpilot.py workbench --help
.venv/bin/python .reqpilot/scripts/reqpilot.py workbench --review-dir runtime-data/review
```

后者 exit 2，报 `unrecognized arguments: --review-dir runtime-data/review`。说明模型想把已保存成果打开来核对，但公共接口没有这条承接路径。不能将该次尝试计为前端已展示，也不能通过接受参数却不绑定成果来假装修复。

本轮实际执行安装版 `workbench` 返回 `NOT_RUNNING`；8000 未发现监听进程。`catalog`确实未列出workbench。因此本轮没有声称完成动态前端验证。只读回执与上述真实失败摘录位于 `prototype/runtime-data/plugin-workbench-fidelity-audit-20260912/`。

## 按顺序高保真拆解：必须明确的含义

现有 `source-map` skill、`business-contract.md`、`tools-map-audit.md`、模型V1已有原序、完整正文、编号粒度、共享条件和表图关系规则。问题是分散且主稿表达抽象，候选稿把原序压成一个词，未作为完整验收目标写明。

主入口应直接要求：**按原件真实阅读顺序，保留完整原文并恢复章节和内容关系，再按原有编号与完整语义拆解。原文、图片释读、工程派生和响应建议分别表达。**

具体含义：

1. **原序**：遵循原件真实位置与阅读关系；保留原编号的跳号、重复和层级。不能按分类/主题重排原文，不能用编号字符串排序代替阅读顺序。稳定 SRC/REQ 身份与显示顺序分开。
2. **文字保真**：保留原文语言、数字、单位、条件、例外、否定和注释；摘要、翻译、改写或推断单独放置，不替代原文。捕获漏字须回查原页或明确缺口。
3. **结构保真**：父级正文、支配条件、编号叶子与跨页续文不能丢；有编号正文默认拆到最小有意义层。BOM/一次完整试验可整体保留，但内部组成、编号、顺序和理由必须可查。
4. **表图保真**：表头、合并关系、共享单元格、备注、续表和原图不丢；试验条件/方法/判据完整。表内图仍属于表格，不单独变成重复图片条目。
5. **覆盖与回查**：每段有意义原文进入正文条目、整体结构或有依据的明确处置；未处理/无法读取范围单列。每个结果可回到原页、范围、表格/单元格或图像；不能只看条目总数。
6. **交付保真**：当前工作台与 Excel 使用同版对象、原序、内容和关系。隐藏/折叠行不等于不导出；看图记录不等于原图已经进入成果。

这里约束的是结果。Agent仍可按实际文档选择逐页Markdown、高清页图、局部裁切、原生文本、OCR或补读等方法；不能把“按顺序”误写成禁止跨页调查、只能机械逐页调用。

## 原有评测漏了什么

上一轮四个新 Agent 只做主 skill 理解复述，没有实际应用/原件，未验证打开正确文档、阶段成果可见、用户反馈绑定，也未要求用原文到条目的例子证明保真。这些未测项应单独列出，不能从“能说出流程”推导通过。

五页离线模型测试核对过原序、完整正文、表图和Excel，但没有通过独立插件完成工作台交互。原生应用之前通过的局部阶段展示，也不能替代独立plugin接入验证。

## 落地顺序与验收

1. **先修业务主稿和入口索引**：把前端核对、原序保真作为交付目标；直接指向真实工作台发现/打开命令与当前文档绑定方法。补catalog遗漏。区分“脚本已有”“尚缺适配”“宿主提供”，不让Agent猜参数。
2. **补独立plugin到现有工作台的最小承接**：以实际项目/文档/来源/版本绑定阶段工件，复用原生已有阶段模型/API；返回可打开的准确地址。确认与反馈回到同一版本的可修订成果。此项要代码实现，不能只补一句“打开前端”。
3. **修分层技能**：原件阅读→原序结构→完整条目→分类/派生分别写出输入、产物和可检查例子。主稿保留底线，细节按需展开。
4. **分别验收**：可理解性；真实工作台打开/阶段显示/反馈修订；原序与高保真；同版Excel；模型耗时与修补成本。材料齐备的阶段测试和原件冷启动分开，仍使用Sol high/Luna max。

本轮产物是查证和修订要求。代码、已安装技能及客户成果未改；现有工作台与来源能力无需重做，但发现的入口、绑定及验收缺口尚未修复。

2026-09-12后继开发增量：主skill/宿主说明已补入口，catalog已列workbench，opener明确返回尚未导入与核验阶段；安装版未替换。独立review到工作台的绑定/发布/反馈仍未实现，原审计中的实际失败继续保留。详见[要求审计实施记录](用户要求追踪与缺口审计.md)。
