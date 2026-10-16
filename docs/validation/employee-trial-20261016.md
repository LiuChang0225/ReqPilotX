# 私有 Web 员工试用接续

2026-10-16。用户选择以 PR #10 `fe97563eb6eec6537f821fd840cbf5a367acee8b` 为基线继续实施，并明确首批入口为现有 ECS 方案的私有 Web。工作在独立工作区，旧工作目录与 canonical repo 的未提交维护文件保持原样；没有推送或切换运行服务。

## 已实施增量

- 核对本机保存的 `99ad73d6` Git bundle 及其 `fe97563e` 前置，将四个已存在的本地提交接入当前候选：Web 账户至交付 UI 回归、固定注册能力包/备份根/systemd 预检、只读实例状态脚本。没有将历史对象退役实现或私有工件混入源码。
- 修正 T05：表格组织与业务条目边界分开；独立里程碑分别处理，同一交付事件的组成可成组，产品组成按原文含义判断。
- 新增 `worked-example --example source-units`，沿公开 TaskService 编制新合成 PDF 的 Map/Items、混合 BOM 与独立条目、跨页完整试验、明确上下文及辅助图；通过真实修订纠正预置分类错误、读回，并展示 preserve 的无新版本分支及程序 Excel。
- 方法资产由 `agent-assets/current/plugin-skills` 维护并同步发现投影。完整工具结果分文件保存，回包仅提供索引；没有把教学答案当作模型或客户语义验收。
- 为原入口命令文档标注单 owner 历史范围，链接当前 SaaS 账户合同；标明 terminal continuation 已发布与真实复验的不同状态。

## 验证口径与现场发现

本轮公开入口首次探针暴露并修正了示范装配错误：TaskService 工作根缺失、READING 选择名、Map edit envelope/文档标题、表请求的 node_id，以及同一条目的编号/上下文更新重复。没有放宽业务校验。新回归的早期 source anchor/注释文件名断言亦按实际工件字段修正。

Node 依赖最初直接链接旧 node_modules，导致 workspace 包解析至旧源码并被 Vite 拒绝；改为复用外部依赖、将 workspace 包绑定本候选后相关 UI 回归通过。该首次失败不记作产品故障或通过。

可复现的相关命令：

```sh
"$PYTHON" scripts/test_python.py plugin --pattern 'test_worked*.py'
"$PYTHON" scripts/test_python.py backend --pattern test_private_web_trial.py
"$PYTHON" -m unittest discover -s deploy/saas -p 'test_*.py' -v
python3 agent-assets/scripts/asset_registry.py --repo . check
npm test -- apps/web/src/auth/AccountToDeliveryAcceptance.test.tsx packages/workbench-ui/src/features/workspace/DeliveryVersions.test.tsx packages/workbench-ui/src/features/extraction/SourceItemAgentLoop.test.tsx
npm run typecheck
```

模型、数据库、浏览器和服务器验收结果在实际执行后续记，不借用前一候选的通过结果。真实原件与模型轨迹留私有目录。正式员工接受由真实使用者完成；Agent 可代为验证浏览器操作路径，但不能冒称员工独立试用。

当前实跑：新旧可执行示范2项、原生工具14项、Web旅程/版本/Agent控件24项、部署预检21项、profile读取/目的地保护2项通过；TypeScript、Web生产构建与资产清单检查通过。BOM原页已渲染检查，表头/行/引导正文可读。新示范最终16条来源、局部分类修订保留全部稳定身份，后续正确结果preserve不产生版本；此计数只属于该合成示范。Ruff最初发现新文件分号及动态import位置问题，修正后单独核验；不修改既有lint规则。

聚焦运行既有 mixed 合同时发现其依赖先前测试的脚本路径注入；补独立 source-checkout bootstrap 后按同一隔离 runner 再验，不改变生产编制规则。

独立审查另发现验收入口的两项P2：worker停止异常会跳过其余资源清理，以及只核bundle而没有核安装服务/Web身份。已改为独立清理和准确失败回执；启动前逐字节核已安装engine/server、拒绝editable/源码回退，校验Web构建的commit/tree/完整文件清单。Web构建新增身份元数据；脏源构建可用于开发，但不能进入本试用验收。新增旧版/篡改/额外代码、清理异常反例进入常规后端/前端CI。未修改原provider配额、工具协议或分类算法。

后续复核发现只停止父进程会留下portable子进程；已为本runner创建独占进程组，停止全组并确认无运行成员后才关PG，强制停止标失败。新增真实子进程忽略SIGTERM/父进程退出的反例，7项试用入口回归通过。全插件302项通过（无失败/跳过）；Web构建身份2项反例通过。首次离线构建因uv缓存缺setuptools84失败，核实既有解释器已安装相同84.0.0后，采用严格offline/no-build-isolation正式工具成功构建；没有下载或修改全局依赖。

## 真实浏览器发现的员工入口阻断

同版b321103安装核576个server文件、engine闭包和Web11个文件通过，API/worker/全新PG实际启动；浏览器登录成功后，项目入口显示`saas_route_not_enabled`。独立HTTP确认账户项目GET=200、全局管理员配置GET=403、runtime能力GET=200。旧UI无条件并行请求管理员配置，项目/文档加载还读旧知识/Set台账/导出接口，导致员工路径失败；模拟旅程曾将这些拒绝接口返回200而漏检。

先令模拟HTTP遵守真实私有profile的拒绝边界，原5项旅程全部失败；再修共享工作台在已确认SaaS模式只请求账户内项目/文档和当前原文Agent接口，跳过未开放的管理/旧台账读取，并移除员工入口中的管理员/知识写操作。没有扩服务白名单或弱化授权；本地/Pair模式继续原读取。相关旅程及Workspace回归26项通过，TypeScript通过。该次环境尚未建业务job或发模型请求（实际job数0）；停止记录与失败证据保留在私有trial-live-1目录，不能记为模型失败或员工接受。

## 真实模型首次请求：认证失败，尚无语义验收

同版`7e9c08e`的已安装engine/server、正式能力包及生产Web通过启动前身份校验。浏览器实际完成登录、项目创建、PDF上传、发起原生文档任务；没有预置项目、任务、结构或条目答案。仅使用本runner生成的1页合成PDF，模型选择沿批准的`qwen3.8-max-0902`，开启思考，不添加新的调用/输出预算上限。

百炼北京兼容端点实际返回HTTP401，任务自然终止为`FAILED / PROVIDER_LOOP_AUTHENTICATIONERROR`，stages为空；`SKIP / USER_SKIPPED`已保存。刷新后同一任务可继续、原文件仍可从实际PDF阅读器读到。该次没有成功模型输出，不能报告来源语义、完整员工链、反馈或Excel通过，不能沿用历史模型成绩。私有`trial-live-2/browser-model-auth-failure.json`保留版本、任务/来源身份和实际步骤；未保存凭据。

用户确认欠费并选择复用既有ECS模型配置，无需在聊天发送key。员工界面将已知认证/权限失败译为具体原因及修复后继续原任务的指引，诊断代码仍可展开；不会自动重试或把已保存任务标成成功。

独立复核另发现条目反馈范围可能沿用之前的“当前文档”。现已将工作台的显式范围请求传入原生控制器，每次条目反馈重设为所选条目，后续手选范围仍可用；请求测试核`source_item_ids`与基线hash，工作台测试核实际范围传递。SaaS旧结构`rendered-reopen`自动写入现已与其它旧结构写操作一样禁用；本地/Pair仍保留原行为。5个相关UI测试文件共139项通过，无失败/跳过，TypeScript通过。

## ECS 只读现场

通过官方已登录Chrome进入指定现有ECS Workbench，沿已启用的免密连接核对。实际`current`指向`reqpilotx-saas-812a4f18fb5e`，manifest源码`812a4f18fb5e057bf8727301893fd74665d4a046`，API/worker均active/running。既有`application.env`与私有`provider.env`存在；脱敏核对显示aliyun、`qwen3.8-max-0902`、官方北京端点、key已配置，但`REQPILOT_AGENT_RUNTIME=none`。没有输出/下载key，没有更改配置、服务、网络或当前发行。

共享Chrome前台窗口同时变化时，待执行脚本曾误贴至另一页面未发送输入框；已确认误贴内容清除，脚本无key值。用户随后明确暂停操作Chrome，重新定位唯一Workbench标签并核对Shell后执行脱敏探针：官方北京端点实际返回HTTP400、`Arrearage`、无模型输出。没有更改服务运行时或当前发行。Python3.11.13/x86_64、可用磁盘约33.6GB，备份根有3个条目；条目存在不证明备份可恢复。

用户已明确“先等待吧”。因此暂停真实模型与ECS推进，待用户恢复额度及明确接续后继续；不自动重试、充值、启用生产Agent或切换发行。私有`ecs-observation.json`记录脱敏现场，未保存凭据值。

## 固定候选、安装及恢复结果

运行候选为`b811e0fdf7bc4289ab9b6bb2159c503a6d455a6a`。正式engine、插件bundle及Web从该干净提交构建，安装身份核验通过576个server文件、完整engine闭包及11个Web文件。bundle为`2a8832377b8f91cc4376712206e980aee42dcea359d8f4e2890d87daff18db5c`；没有源码或editable应用回退。Mac Python3.13.2的隔离venv复用既有依赖路径，不代表全新机器或Linux安装。

该候选23项真实PostgreSQL验收全部通过（19.265秒，无跳过），包含真正SDK/公开工具/worker编制修订交付、两账户授权、版本与实际Excel。模型网络使用`httpx.MockTransport`，不是本轮真实provider成功。

已安装wheel的独立恢复测试也通过：真实PG导出并恢复到另一数据库，复制639个application文件并核hash，核验时原application目录移开；同账户会话、原件和冻结Excel字节一致，runtime jobs相同，另一账户导出被拒，恢复的注册bundle重新核验通过。首次适配验证脚本漏改一处旧能力包路径而报FileNotFoundError；修正脚本路径后以新输出目录复验，不修改产品或放宽断言。两次临时PG均已停止。

Linux3.11/x86_64/glibc2.28离线发行包已完成：两个wheel、59个从既有已校验存档复用的依赖wheel、正式能力包、生产Web与运维文件，288个payload文件校验通过。未新增下载。归档`reqpilotx-private-web-b811e0f-linux-py311.tar.gz`为69,485,627字节，SHA256=`ef6521e25f77f85fdab999141d3237000cddd351f83c1c409bca453087b8167f`，保存在本轮私有runtime-data。没有上传/安装/切换到ECS，Linux目标运行和实际服务器恢复仍未验收。

浏览器两次隔离环境的API、worker、所属进程组和PG均完整停止，cleanup_errors为空。原有两个工作目录的用户变更保留，远端未push。
