# 目标态核对入口（阶段一实验）

一个独立、仅本机运行的原文核对工作面。读取 Agent 已明确写出的来源绑定结果，展示原文、
首版判断、代理复核候选、派生草稿、疑点及原页证据，并记录版本绑定的人工反馈。

**不是通用自动提取器，也不是已验收插件。** 当前 F06 的语义决定来自主 Agent 直接阅读；
材料化脚本不做语义推断。接受业务结果后，再按[开发计划](../../docs/需求提取/开发计划.md)沉淀可移植能力。

## 启动

只需要 Python 3.10+；没有 npm 安装、前端构建、数据库迁移或 App Server 前置。

```bash
python3 prototype/target_review/server.py --run-dir /absolute/path/to/private-run --port 8770
```

访问终端打印的本机地址。现有 React/FastAPI 应用与本入口相互独立，不改现有 PDS/F06 数据。
一个反馈目录只运行一个写入服务；多窗口通过同一服务核对。不要把端口转发到公网。

运行目录包含：

- `source.pdf`：原始 PDF 的固定副本。
- `review-bundle.json`：带 SHA-256 的实验工件，`schema=reqpilot-target-review/v1`。
- `pages/p-01.png` 等：按工件 PDF 页号命名的真实渲染，不用占位图冒充原文。
- `feedback/reviews.jsonl`：首次反馈后产生，追加式保存，不覆盖原文及 Agent 初稿。

输入工件包含 `source/pages/outline/items/tables/lines/issues/spotlights`；代理候选可增加顶层及逐项
`proxyReview`，派生项以`origin=agent-proxy`标识。原文条目与派生项分别有身份；
表格保存真实单元格边界与经 Agent 核对的表头角色。当前格式用于实验，不视为冻结分发协议。
源 PDF 或工件指纹不符、反馈日志损坏/错版本时服务拒绝加载，不截断日志、不静默重新初始化。
服务启动时固定读取一个工件revision；如果重新材料化`review-bundle.json`，必须重启服务，
并核对`/api/bundle`与页面“来源与工件身份”显示的是同一新revision。

## 工程师核对

- 沿同一原文树查看条目；1:N派生要求直接生长在对应原文父项下，可从左树逐条选择、核对、修改和确认。整表显示所有片段，业务行继承真实合并单元格和适用表头。
- 原文、判断和派生表达分开。点击单元格精确定位；派生证据按实际精度标明条款级或图级。
- 单元格带`visualRefs`时在原生表格单元格内显示来源页裁剪并可回跳；无效坐标明确显示证据缺口。页眉优先使用工件`printedLabel`，缺失时不推算文内页码。
- 内容角色支持修订记录与示例；它们保持可见但不因此进入需求筛选。
- 代理处置、首版→候选分类差异、共同适用范围与待澄清状态可见；代理caveat单列为“非要求正文”。
- “本轮代理复核”可筛出候选范围；代理、QA沙盒和人工确认使用不同状态与计数。
- PDF 页号与文内印刷页号区分。1100px以下切换内容/原页，宽屏并列；目标为≥1024px桌面。
- 原文分类与派生内容可另存修订；原始客户文字不能被编辑覆盖。问题、改动、核对及撤销均绑定对象与版本。
- 确认一条原文不自动确认派生项。派生项问题、待复核修改和待补证据会汇入关注筛选。
- 未保存编辑阻止确认/撤销；当前页面内切换条目保留草稿。刷新前提示未保存内容，**不承诺跨进程保存未提交草稿**。
- 并发反馈更新后刷新有效正文/分类并要求重新核对；分析版本变化时停止旧页提交，保留草稿供复制。
- 写入结果不确定时停止该服务后续写入；重启会验证并重放现有日志。不会反复追加不确定请求。

`GET /api/export` 导出来源、结果版本、有效反馈和完整事件历史，不包含写入令牌。
它是核对记录，不是正式 ReqIF/Compliance Matrix。

## 验证

从仓库根执行：

```bash
python3 -m unittest discover -s prototype/target_review -p 'test_server.py'
node --test prototype/target_review/test_review_core.mjs
ruff check prototype/target_review/server.py prototype/target_review/test_server.py
git diff --check
```

测试使用合成数据，不向真实案例写入模拟人工验收。浏览器交互验证另用独立目录：

```bash
python3 prototype/target_review/server.py --run-dir /absolute/path/to/private-run --review-dir /absolute/path/to/private-qa --reviewer-kind qa --port 8771
```

当前实跑：54项Python测试、31项浏览器无关JS测试通过；F06代理候选另有基准/来源/身份/状态完整性校验。
真实浏览器验证包括代理范围筛选、业务行/表头、单元格定位、caveat、1280/1024布局、
保存/刷新/撤销和父子独立；冲突与旧版本拒写由自动测试覆盖。
自动检查不提供语义正确率；用户业务验收仍待完成。

六份案例的阶段审计显示，各隔离任务曾在自己的worktree补充页码Enter、有意空白页、需求范围、
三位页图、四维语义摘要等能力；当前主工作台尚未完整吸收这些增量。尤其长文档页图不应再从页数和
固定补零规则推断，应消费工件`pages[].renderPath`。在统一回归完成前，本入口只代表当前F06工作面，
不宣称可无损打开全部六份候选。详见[阶段审计](../../docs/需求提取/六份典型案例阶段审计与复盘.md)。

## 文件职责与安全

- `server.py`：固定资源白名单、来源指纹、同源请求检查、反馈持久化与冲突控制。
- `static/review-core.js`：合并表格投影、证据范围、有效修订和关注判定。
- `static/app.js`、`app.css`、`index.html`：交互与展示，不决定文档语义。

无模型调用、远程字体、遥测或外部服务；只绑定 `127.0.0.1`，不输出客户正文或令牌到访问日志。
完整客户内容、页图、案例专用脚本、截图和反馈均放被忽略的私有运行目录，不进入 Git 或插件资产。

## 复用主应用UI（2026-09-06）

当前PDS范围通过可选的React适配模式复用正式前端组件，不再使用单独报告页作为核对入口。先构建主应用，然后启动本地工件适配服务：

```bash
npm --prefix prototype/frontend run build
prototype/.venv/bin/python prototype/target_review/server.py --run-dir /absolute/path/to/private-run --frontend-dir prototype/frontend/dist --port 8784
```

打开 `/workspace?task=extraction&mode=review&reviewCase=<bundle.ui.caseId>`。候选bundle需包含明确的`reviewMetadata`类别路径、主题、来源及已有反馈说明；适配器不推断分类、不调用模型、不创建或覆盖生产数据库里的项目/Requirement Set。

复用的组件为WorkspaceShell、RequirementTreePanel的review入口、RequirementMatrixWorkbench、MatrixSourceContent和PdfViewerPage。新增CandidateReviewWorkspace仅绑定当前工件上下文；反馈沿用ReviewStore的追加日志、来源/版本校验与撤销。

`GET /api/candidate-review`提供范围与文档元数据；匹配当前项目/文档/来源版本的Matrix、节点详情、来源页和geometry-focus读接口由react_contract.py投影。PDF仍按原文件提供。此虚拟上下文不冒充正式项目入库。

类别与受控主题取交集；上级作为背景保留。`POST /api/candidate-review/export`接收UI按同一树遍历得到的直接命中ID、来源版本和反馈revision，返回可筛选XLSX，含大类、系统需求类型、主题标签及原文/派生/父项。原文长引用清单另有参考依据表；标记为SOURCE_TABLE的其它原表使用独立原表工作表，保持实际列、行/列合并，不增加需求行。header_rows明确的表头不计入逻辑记录数。写入令牌不导出。

React模式的CSP仅额外允许当前构建中内嵌初始化脚本的具体哈希、同源PDF框架及worker；未使用任意inline脚本放行。默认静态核对模式与写入保护保持。

本轮验证包括58项Python（含新适配4项）、108项聚焦前端测试、构建，以及隔离QA浏览器的筛选/反馈/刷新/撤销/导出和1680/1280/1024布局。QA与真实反馈目录分开；案例核对不等于全文验收。

2026-09-06第5章增量补充：59项Python通过（适配5项）；100项受影响前端测试与构建通过。修复列表过滤缩小时旧滚动位置使匹配行不可见，保留正常惯性滚动。具体客户候选及浏览器回执留在私有main-ui-chapter5-20260906运行目录；UI样式调整后置。

原文保真修订：候选可携带sourcePresentation的标题来源范围/原编号，以及sourceExcerpts中的逐行字符范围。适配器直接读取bundle.lines形成标题和引用，不接受改写文本充当原文摘录；派生PDF框标由经原PDF字符匹配的sourceRefs提供。原文已有标题/编号优先；原文无独立标题的逻辑拆分可显式标记AGENT_ASSIGNED，UI与Excel显示“Agent 拟名/编号”，不冒充来源标题。Excel增加原文标题/摘录及派生依据两列，父编号可追到原章节；前版本真实反馈单独绑定保留，不改签为新版本确认。

候选分类与分析就绪分离：reviewMetadata.analysisEligibility可显式设为NOT_ELIGIBLE，而requirementness与classifications仍保留客户条目的需求/类别判断。例如已识别质量类、正文待后续定义的条目仍能进入质量/需求筛选，但不产生已有判据或分析结论的假象；适配器不据标题自动推断状态。

1:1结构与原文合并也适用于含下级原文条款的父项：子章节通过原结构别名挂回该原文父项，保留SOURCE_ITEM与DERIVED_REQUIREMENT区别。详情路径可找到上级直接原文，避免把其适用条件与下级来源条款分离；结构层级不是工程派生关系。

原文父子结构只用于来源导航。React原文详情默认展示所选原文；仅candidate_review.applicabilityScope显式指向的作用域原文共同显示，未声明或不可解析时不退回通用祖先。派生项仍显示真实原文父项。此选择不删除背景，也不改写冻结bundle。

上下文读取不受UI展开方式限制：节点详情path保留完整章节祖先及正文，Agent应先确定位置与对象再理解本条。applicabilityScope只影响默认共同显示，不裁剪分析上下文。ui.reviewVersion/versionLabel可为冻结审阅工作版提供可读编号与名称；它们不冒充正式发布基线。

审阅布置表使用可选reviewTables（Agent选定的原文记录、原始物理段、表头绑定、图片关联）；适配器仅投影，不按所有表行自动生成需求。表格行以TABLE_RECORD呈现，原文身份与派生身份保持；完整表位于原章节节点，不计入原文条目数。

原图沿用主UI的`extraction/source-crop`读接口：校验当前source_sha256、页号和有限页内bbox后，用固定PDF渲染PNG。React模式使用项目venv中的PyMuPDF；Excel图像使用Pillow/xlsxwriter。文档及source-pages返回组件所需的来源指纹。跨行图独立保留一次并关联source_row_indexes，选择任一相关行可查看。

原表片段和原图在XLSX辅助工作表中按来源关联输出，主清单仍只含直接命中条目。图片不改写为文本，图内转写必须保留图像精度证据，不能声称字符级原生文本覆盖。

## Agent反馈上下文桥接

`loop_context.py`读取现有主应用服务的固定bundle和有效反馈，生成任意Agent宿主可消费的输入。当前由工作Skill引导Agent处理；它不自动调度模型、不发布修订，也不把未核对项一律送人工。

```bash
python3 prototype/target_review/loop_context.py --url 'http://127.0.0.1:8790/workspace'
```

使用实际运行URL。`--target`聚焦对象但保留完整来源访问，`--history-url`读取同PDF旧版有效反馈并保持原revision。`--output`只创建新私有快照，不覆盖现有文件；输出不包含会话写入令牌。QA需显式`--allow-qa`，不能冒充负责人反馈。下一修订必须重读feedbackRevision，不能用旧快照覆盖新反馈。

5项聚焦测试覆盖版本/来源错配、原文修改、历史认可不扩散、QA隔离、范围与完整原文访问；真实读取已覆盖PDS修订06/07及修订02的5条历史反馈。使用与范围见[闭环工程说明](../../docs/需求提取/闭环工程与双路径实施.md)。

## Agent 自查后的版本修订

`baseline_loop.py` 应用 Agent 明确编写的决定，将已授权的输入候选形成新的本地工作版本：

```bash
prototype/.venv/bin/python -m prototype.target_review.baseline_loop \
  --input '<private-input-run>' \
  --decisions '<private-decisions.json>' \
  --output '<new-private-successor-run>'
```

决定文件包含 `sourceRevision`、`caseId`、`scope`、`initialNodeId` 和 `decisions`；每项使用 `targetId`、`expectedTextSha256`、`metadata`、`evidence`。可替换派生集合或补充 `associations`，原文 text 不可改写。可选 `feedbackInputs` 绑定原反馈文件哈希；可选同源 `overlay` 保留已有工作版和原版本认可，并明确列出退出活动清单的旧身份。变化前对象、输入快照和决定文件写入私有差异记录。

工具检查来源身份、定位、结构和原文保留，并将已声明的捕获坐标转成 PDF 点；只投影已经捕获的表格，不定义语义边界。`reviewMetadata` 是新版本的当前分类依据，原 `class/reason/proxyReview` 为历史记录。`loop_context.py` 提供当前候选、关联验证、源疑点和原版本认可；没有把历史反馈重绑定为新确认。

本地材料化成功不代表自动模型调度、应用内历史版本切换、生产数据库发布或已完成符合性响应。实跑范围见[多文档基线B1](../../docs/需求提取/多文档提取基线B1.md)。

### 源顺序合同

结构节点和原文条目采用同一来源位置空间；缺失结构位置由 `source_order.py` 读取标题/内容/表格证据，禁止以数组下标或章节字符串兜底参与比较。`baseline_loop` 冻结缺失位置并检查投影树的全文原文遍历。章末单条内容不能替代章节成为早先子章节的父项；已有完整表格/共享说明关联继续承载表格行。界面排序和导出应从同一树验证，不能只检查哈希、行数或搜索结果。故障复现与证据见[源顺序修复](../../docs/需求提取/B1源顺序回归与修复.md)。

## 原生 SourceItem 后继候选（Codex 直接执行）

`native_candidate.py`接收来源保真的typed后继包，直接复用`build_delivery_snapshot`、原Document Map/SourceItem、原表/图片、显式派生与成熟Matrix导出器。`CandidateReviewWorkspace`继续使用同一原文树、内容/PDF和反馈组件；不经B1 outline重建结构，不替换主应用当前基线，也不制造App Server回执。

```bash
PYTHONPATH=prototype prototype/.venv/bin/python -m prototype.target_review.server \
  --run-dir '<private-native-candidate>' --native-source-items \
  --frontend-dir prototype/frontend/dist --port 8812
```

候选目录的`native-review.json`声明`native-source-item-review/v1`、真实`CODEX_APP_DIRECT`来源、动作记录/时间/模型、项目/文档身份、前版SHA以及输入文件SHA。输入文件为`source.pdf`、`document-map.json`、`baseline-previous.json`、`baseline-candidate.json`、`predecessor-validation-receipt.json`、`classification-decisions.json`、`table-previews.json`、`decomposition.json`、`annotations.json`、`semantic-notes.json`和`review-issues.json`。

导入器根据前版和明确分类决定重新编译候选，并复核同源Map、PDF和派生证据；单纯重签被改写的候选不能通过。此入口目前支持保留原始SourceItem边界的分类修订和显式派生；不宣称支持任意来源拆合或生产晋升。原App Server发布路径的严格回执验证保持。

原生候选显示SRC/REQ/Snn、前后分类依据、历史REQ与未解决来源问题，SRC可搜索，“需关注”可筛真实来源问题。原生候选导出始终使用完整来源Matrix；反馈控制字段不混入导出领域schema。反馈复用append-only日志、source/result版本与乐观锁，测试用QA身份与真实工程师日志分开。`loop_context.py`可读取该入口，并携带一次完整Map及原表上下文；来源可见性不受分类或UI展开影响。

原生投影使用顶层`logical_table_id`，旧投影使用`continuation.logical_table_id`；前端规范化兼容两者，使同一BOM/试验记录的所有分页保持一张逻辑表，而不截断当前记录。聚焦回归见`test_native_candidate.py`及前端`MatrixSourceContent.test.tsx`。
