# 按文档保存任务并接续

用于独立插件的新委托。已有原生任务继续使用host给出的真实工作目录；已有review保留其版本与反馈，不先搬目录。`bootstrap`为环境准备一次工具和skill，`workspace init`才是每份文档的新任务入口。

## 第一次开始

```text
<python> <plugin>/scripts/reqpilot.py workspace init --working-root <私有工作根> --project-key <本地项目键> --document-key <本地文档键> --source <原件.pdf> --goal "<用户真实委托>" --workbench-url "<已选项目和文档的真实工作台地址>"
```

先在工作台选项目、添加对应原件。程序按原件SHA核对应用文档，返回准确`task_root`、TASK入口和工作台链接；有浏览器能力时打开并检查文档。应用离线或还未选文档时可以省略URL先保存任务；稍后用 `workbench --workspace <task_root> --url "<真实文档地址>" --open` 连接，不能凭本地目录键猜应用ID。

同项目多文档使用不同document-key；同一原件的新独立委托可再次使用同project-key/document-key执行init，程序创建新的work-id，不混成旧任务。接续旧委托用status，不重复init。新任务默认自主完成请求范围，不要求Map人工确认；只有用户明确要求先确认结构时，在init使用`--require-map-confirmation`。已有任务与绑定保留原确认策略，不因新默认值被静默改写。

## 程序填机械参数，Agent提交判断

```text
<python> <plugin>/scripts/reqpilot.py workspace run capture --workspace <task_root>
<python> <plugin>/scripts/reqpilot.py workspace run reading-workspace --workspace <task_root>
<python> <plugin>/scripts/reqpilot.py workspace run compile-map --workspace <task_root> --input <结构判断.json>
<python> <plugin>/scripts/reqpilot.py workspace run compile-items --workspace <task_root> --input <条目判断.json>
<python> <plugin>/scripts/reqpilot.py workspace run deliver --workspace <task_root>
```

这不是固定调查算法。Agent按阶段skill选择阅读/裁切/检查方法，先解决原文含义、顺序和边界，编制时只给所需决策。程序从当前已保存工件填source、reading、完整Map、baseline等路径，分配新output目录并冻结决策输入。`run`也支持compile-images、compile-annotations、compile-decomposition和review-open。参数细节用`workspace run --help`；显式输入仍按调用时cwd解析。

每个有效范围完成就编制保存、自查修正并继续，不等整本结束，也不把渐进展示变成等待人工反馈。仅明确要求确认的Map尚未确认时，后续Items/图片/review-open会等待；仍可回查和修订原件相关内容，不伪造确认。`workbench-pull --workspace <task_root>`取回实际介入的反馈或确认，不是默认继续下一阶段的前置。

## 保存位置与继续条件

| 位置/返回 | 用途 |
|---|---|
| `TASK.md` | 人可读的委托、来源、当前成果、剩余范围、下一动作及固定工具入口 |
| `source/`、`reading/` | 原件，以及每次capture/逐页Markdown/高清图/表图证据包 |
| `inputs/`、`outputs/` | 冻结的决策输入、每次完整编制bundle；不手拆Map依赖 |
| `operations/`、`operation-receipt.json` | 实际操作和最后提交证明；文件存在不等于成果完整 |
| `workspace-index.json` | 程序生成的当前选择与候选引用，不手改为“完成” |
| `review/`、`host-receipts/` | 既有review工作版、独立的真实工作台同步回执 |

COMMITTED表示本地成果保存通过；还要看workbench_sync是否真的同步。SAVED_CANDIDATE/CANDIDATE_CONFLICT保留成果但不替换当前工作版，也不自动发布。FAILED/BLOCKED保留操作和诊断，不必重新阅读或重做已有效范围。单独释读/拆分overlay先落本地，review-open汇入当前选定overlay后按现有支持范围同步；不能说每个本地工件已经前端可见。

```text
<python> <plugin>/scripts/reqpilot.py workspace status --workspace <task_root>
<python> <plugin>/scripts/reqpilot.py workspace recover --workspace <task_root>
```

在task_root或其reading等子目录中可省略`--workspace`；显式workspace不合法时不会退回别的任务。同原件不同任务的默认/custom binding分别保存，换cwd仍绑定原任务。只有source或Map也能接续，不以Excel为前提。恢复不按mtime选择赢家。

默认返回当前精确路径、剩余范围和本次结果；需要调查完整历史索引时再加`--full`或打开返回的index文件。无需每次把所有历史JSON重新读进上下文。

已有review继续按[修订方法](revision-loop.md)使用review-packet/review-revise/review-close，附`--workspace <task_root>`。review-close仍检查已有review与同版交付，不另造workspace成果类型。Map快照仅用于展示；status给出的完整编制目录才可重编，UNAVAILABLE时明确缺失依赖。

首次或无内容改动的交付直接使用 `workspace run deliver`：当前owner是review时，程序在COMMITTED后自动核验并登记导出，返回`delivery_attachment.status=CURRENT_DELIVERY_ATTACHED`。接着`review-verify`即可交接，不制造空candidate。`ATTACH_FAILED`不删除已导出Excel，也不声称登记完成；已有导出的精确补登记用[review-attach-delivery](revision-loop.md#register-an-unchanged-current-delivery)。真正修订candidate才需要review-close。

任务记录实际工具入口、Python环境和bundle摘要。接续使用原固定包；找不到就恢复该包，不静默换成新安装版本。开发checkout明确标为未冻结，不代表已发布插件。正常导出由内部程序执行，不增加模型全文阅读。
