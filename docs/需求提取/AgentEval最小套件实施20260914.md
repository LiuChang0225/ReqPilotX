# Agent Eval 最小套件实施

更新：2026-09-14。按用户确认建立统一评测入口，两条辅助支线已按用户要求用 `xhigh` 接续；这项档位调整不改变提取模型对照的 Sol high / Luna max 配置。

## 结果与边界

已落地 [Agent Eval 套件](../../prototype/eval/agent_evals/README.md)和 `prototype/scripts/agent_eval.py`，提供 validate、prepare、run、import、review-template、grade、report。执行复用固定插件的 bootstrap、Codex prompt discovery、CLI/MCP 和业务核心；没有重写提取算法，没有迁移到另一Agent平台或更新生产插件。

首批登记 3 个案例条目：同一已知五页合成原件的 Map-only 与完整流程两个 development scope，以及 PENDING_INPUT 的真实复杂留出占位。当前只有 1 份已就绪原件，不能以 3 个case宣称跨文档能力；此前 [29 个标准案例](../../prototype/eval/extraction_cases/README.md)保留为上游来源，尚未全部转换为此入口可运行的独立Agent案例。

8 个维度、27 项检查已定义：5 项程序检查自动执行；16 项来源语义与6项过程判据需要明确评核记录。来源身份/固定包/工件完整性/所需交付存在/自然终态，与真实业务质量分别记录。手工或Agent填写的语义评核绑定 run、原件和工件版本，并需要可核验文件引用；未评项保持 REVIEW，不伪造专家接受。

## 与官方指导的对应

[Agent Evals 指南](https://developers.openai.com/api/docs/guides/agent-evals)强调先用执行轨迹定位问题，再形成可重复数据集与评测。本实现将已有逐次诊断收敛为这两个可持续入口；尚未接入自动模型裁判或批量重复统计。[评测最佳实践](https://developers.openai.com/api/docs/guides/evaluation-best-practices)要求任务相关案例、自动指标与专家经验校准、持续补充边界；正常正文、整体BOM、跨页表/图、父项/派生、无内部依据、原序及同版交付已成为明确判据。

不将固定工具顺序、最少调用数、旧SRC/REQ总数当成正确答案。必要视觉回查仍可自由进行，缺输入/合法解释分歧与真实错误分开。专家经验用于离线校准评分，不意味着生产运行逐阶段等人工批准。

官方已公告 Evals dashboard/API 的退出时间，并提供 [Promptfoo 迁移路径](https://developers.openai.com/cookbook/examples/evaluation/moving-from-openai-evals-to-promptfoo)。本轮保留可移植数据/评分及本地Codex执行器；未安装Promptfoo，也未上传客户材料到托管评测服务。

## 已验证

- 案例/rubric JSON Schema、ID及引用一致性通过；未准备留出原件不能执行，私有路径与评核不投喂被测Agent。
- 轨迹按 segment/thread/turn occurrence/item ID 去重，支持续接ID重置；不将 started/completed 双计，不输出隐藏reasoning、命令或错误正文。实际图片字节、仅路径、缺usage、缺终态分别报告。
- 保存工件校验原件、index、receipt和文件内容；候选/冲突/未完成操作不算有效首件。历史截止时点若被后续工件污染，会标证据不适用，不用新head覆盖旧成绩。
- 新runner没有整次时长杀进程，默认20分钟仅写明确的非原子观察；测试用真实子进程验证观察后仍自然结束。仍保留单工具技术超时和用户中止语义。
- 只读接入5份历史观察：Map新/旧接口、宿主审批受阻、Luna完整流程截止版、Sol完整交付版。已有调用数49/86、83/67及首件等可核验指标保持原值；宿主失败单独标 ENVIRONMENT_BLOCKED。
- 对BOM正反例补了操作者来源评核：Luna截止版将完整配置拆为支配语句及3个部件SRC，whole-unit-granularity=FAIL；Sol将完整配置保留、跨页V-01成组且V-02独立，该判据PASS。**这是有来源引用的操作者复核接入，不是已实现自动BOM语义裁判。** 其余新rubric未判项仍REVIEW，不推翻旧报告按旧范围已有的结论。
- 已用实际安装103716包完成新Luna full任务的Codex隔离prompt/技能/真实MCP工具发现预检，零模型调用；没有为本轮再运行完整模型。未绑定新工作台的预检目录只能证明准备能力，实际run需新建带隔离文档绑定的准备目录。

21项聚焦回归与Ruff通过，包含来源/版本篡改拒绝、报告不写入被测工作区、程序全绿不冒充语义成功、观察不截断、实际宿主approval文案、候选不算首件等。没有为评测框架修改重跑全部产品测试。

## 当前入口与下一步

公共代码/合同在 `prototype/eval/agent_evals/`，命令示例统一在该目录 README。私有原件binding、历史导入、语义评核、报告和预检在 `prototype/runtime-data/agent-evals-20260914/`，代表报告 `verified-comparison-final.md`。旧原件、包、模型日志和结果只读保留，未提交/推送或改全局配置。

后续先收敛 Items 编制；工具/skill每个实质增量跑相关程序与Agent scope，完整发布候选通过本入口做不中途截断的完整冷启动，再补真实留出原件和重复运行。优先校准少量高价值语义判据，确认自动裁判与已有专家经验一致后才扩大；不用增加模型调用量替代案例和评分质量。

生产插件仍为103716；本轮只是仓库评测工具新增，没有重载原生应用服务、改前端或客户成果。结构缺号finding处置、BOM来源提醒、Office等既有产品待办不因评测套件就绪自动完成。
