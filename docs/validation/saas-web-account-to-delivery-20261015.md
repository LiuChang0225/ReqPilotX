# SaaS Web 账户到交付：离线界面旅程回归

日期：2026-10-15。状态：**离线 UI/请求合同通过；非真实文档评测、非模型质量、非员工或云端验收**。

## 范围与来源

本轮承接生产协作台原始需求 `beac701f-9582-4ef1-87f7-363905019487`「ReqPilotX的产品本体开发与完善」（2026-10-12 10:41:39 UTC，submitted/version 1/high）。原文要求接管本地开发、复用历史 session、先看状态，再对具体不清晰处 grill。原始字段由父会话核对并交接，本轮未登录协作台。

父会话提供的已有目标为 SaaS 界面账户 → 上传 → Agent → 导出，先单人后邀请制，用户与文件/结果私有；不再重复询问这些产品方向。本次授权只增加离线旅程回归及专项验证记录，不运行真实模型、客户原件、部署或数据库迁移。

## 固定候选与依赖

- 源提交：`fe97563eb6eec6537f821fd840cbf5a367acee8b`。
- 源 tree：`177a40e69a50dd9dd9f027589db4fffc3295d2b3`。
- 从已存在的本地 Git 对象生成完整 archive，解出全部 2,679 个 blob，保留跟踪的符号链接；逐一复算 Git blob identity 和 SHA256，与该提交一致。
- 这是完整源码快照，没有 Git 工作树元数据；不冒称正式发行构建或新提交。
- 使用现有 Node `v25.6.1`、Vitest `4.1.10`、TypeScript 和已安装依赖；无下载、无安装、无新增依赖锁。
- 依赖复用通过隔离目录中的链接完成；`@reqpilotx/workbench-ui` 单独指向隔离候选，不能沿用指回旧主工作区的 workspace 链接。
- 在候选目录执行 `import.meta.resolve('@reqpilotx/workbench-ui/web')`，实际解析到候选的 `packages/workbench-ui/src/web.ts`。读取其父目录之外的第三方依赖属于复用依赖，不是复用旧业务源码。

创建前后证据保存在候选的外层验证目录：`source-integrity-before.json` 与 `source-integrity-after.json`。后者重新核对全部 2,679 个既有 blob，漂移为零。原主工作区 HEAD 仍为 `f18cf939adc208e7b0ed72d6d4e667b52fa8ee75`，11 个维护文件 SHA256 全部不变；本轮未移动、暂存或提交它们。

## 精确文件边界

只新增：

1. `apps/web/src/auth/AccountToDeliveryAcceptance.test.tsx`。
2. `docs/validation/saas-web-account-to-delivery-20261015.md`（本文）。

没有生产代码修复；所有既有候选文件保持原字节。两份本地接续规格另在主工作区按原始需求字段更新，不属于本测试补丁。没有修改 PAIR。

## 测试接缝和 fixture 含义

新测试渲染真实 `App`，组合账户门、项目管理、上传队列、工作台路由、文档 Agent 协作和原生交付版本界面。没有 mock 业务组件、workspace context 或共享 API 模块。

替身只在网络边界：全局 fetch、XMLHttpRequest 和 SSE 可用性。fetch fixture 只允许同 origin，未登记的路由记为意外请求并使断言失败；XHR 是内存 EventTarget，记录 multipart command ID、CSRF、进度、完成/丢失回复/取消；SSE 不可用时走真实界面的轮询分支。

上传的文件只有“UI transport fixture; never parsed”字节和 `.pdf` 文件名，不是业务文档，不被解析。原件 SHA256 独立以 Node crypto 从这些字节计算，用于核请求身份。页面显示的 delivery/task/version 是明示的服务响应 fixture，条目和需求计数为零，没有构造真实提取答案。下载响应也是明确标注的 UI 字节，不是有效 XLSX。

因此，本测试证明的是组合界面的可见状态、请求来源/版本绑定和交互防重；它不证明文档提取质量、业务语义、真实导出器生成的 Excel 内容、服务端权限或浏览器实际下载行为。

## 覆盖

| 用例 | 关键可观测断言 |
| --- | --- |
| 账户到同版草稿 | 登录前无项目请求；登录处理中重复点击仅一次 POST；创建项目、重复选同文件各一次；精确上传 URL、multipart 文件/大小、XHR cookie 模式及回执匹配；native extraction-loop 请求固定 source/goal/origin；交付原文 v2/响应 v1；保存请求固定 source/delivery/business-basis 哈希和 command ID；业务 POST 带当前 CSRF，fetch POST 为 same-origin credentials；下载链接固定 versions/1/export |
| 上传回复丢失与重载 | XHR 在 fixture 已保存后报错；真实 entry-command 客户端读回原 import receipt；不重新上传；重载真实 App 后来源仍可见 |
| Agent 暂停与继续 | 完整 project/document/task URL 暂停与继续；取消保持原 job，继续形成新 attempt；暂停/继续响应均保持处理中以验证双击仅一次 POST；start/interrupt/resume 命令 ID 互异；继续后可保存固定交付版本 |
| 会话过期与换账户 | 实际账户门卸载私有工作区，上传 AbortController 触发 XHR abort；另一账户登录后，向旧 XHR 强制发送迟到成功 load，旧项目/文件/交付仍不显示。只证明 UI 卸载/隔离及此 XHR 事件情形，不证明服务端跨账户授权 |
| 交付版本不匹配 | 预览 source 与当前来源不符时展示错误，不显示保存动作或下载链接，不发送交付保存请求 |

交付保存的防重同样在响应保持未完成时验证，避免把完成后的第二次有意保存误判成重复提交。

## 实际验证

| 检查 | 实际结果 |
| --- | --- |
| 新增旅程文件单跑 | 5 PASS，0 FAIL，0 SKIP；最后单跑 2.93 秒 |
| 最终相关回归 | 11 文件 / 129 PASS，0 FAIL，0 SKIP；3.89 秒 |
| 最终 TypeScript `--noEmit` | PASS，退出码 0 |
| 候选既有源码一致性 | 2,679 blob，0 漂移 |
| 主工作区保留 | HEAD 未变，11 维护文件哈希未变 |

最终回归包含新增旅程，以及账户门、storage、transport、App routing、SaaS availability/native entry、项目管理、upload queue、project/document client、delivery versions。各轮成绩包含重复用例，不能把 5 和 129 相加。

保留实际失败：首跑 5 项失败，原因是测试关闭上传窗口的选择器匹配多个按钮、遗漏 worker-fleet 的只读 fixture，以及换账户后错误假设必须重新出现首项目创建按钮。第二轮仍有 5 项失败，另暴露遗漏的 native-reference/历史运行只读 fixture，及“模拟响应瞬间完成后再双击”会合法生成第二次保存/继续的测试假设。修正仅涉及新测试 fixture、选择器与响应时序，没有改生产实现或放宽断言。第三轮新增 5 项通过，随后补充 attempt 身份及展示版本断言，最终 129 项回归通过。

新只读代理审查确认业务组件组合与候选 workspace 解析，同时指出上传 URL、start body、控制完整路径、cookie 传输断言不足，以及先前过期用例未真正发送迟到 load。这些均在新测试内补强。代理随后只读复核补强已落地，并建议三个 command ID 在互异检查前均须为有效非空字符串；该断言已补上，最终再跑 129 项通过，TypeScript 再通过。代理未运行测试，不将其只读审查计为额外通过数。下载仍明确限定为 href 加 fixture 字节读取，没有把它提升为浏览器下载或有效 XLSX 验证。最终测试 SHA256 为 `c01f1dd5bd16c3af0faf4a16cc05969371bce0ea3d85dbc8ba48f8b175405463`。

## 复跑

在与上述提交相同的完整候选和已准备依赖中执行；`VALIDATION_NODE` 指向现有 Node，不会下载依赖：

```sh
"${VALIDATION_NODE:?Set an existing Node executable}" node_modules/vitest/vitest.mjs run --config vitest.config.ts \
  apps/web/src/auth/AccountToDeliveryAcceptance.test.tsx \
  apps/web/src/auth/AccountGate.test.tsx \
  apps/web/src/auth/StorageAcceptance.test.tsx \
  apps/web/src/auth/TransportAcceptance.test.ts \
  apps/web/src/app/App.test.tsx \
  packages/workbench-ui/src/features/extraction/SaasAgentAvailability.test.tsx \
  packages/workbench-ui/src/features/extraction/SaasNativeEntryAcceptance.test.tsx \
  packages/workbench-ui/src/features/projects/ProjectManagementPanel.test.tsx \
  packages/workbench-ui/src/features/documents/uploadQueue.test.ts \
  packages/workbench-ui/src/shared/api/projectDocuments.test.ts \
  packages/workbench-ui/src/features/workspace/DeliveryVersions.test.tsx
"${VALIDATION_NODE:?Set an existing Node executable}" node_modules/typescript/bin/tsc --noEmit
```

日志在外层验证目录的 `first-run.log`、`second-run.log`、`third-run.log`、`focused-regression.log`、`focused-regression-final.log`、`focused-regression-delivery.log` 和 `typecheck-delivery.log`。候选业务源码必须从此完整快照解析；不要只拷新测试到仍停在 `f18cf939` 的主工作区运行，也不要让 workspace 包链接回旧源码。

## 未运行与后续

未运行真实典型文档、模型/视觉/抽取语义、独立员工操作、真实浏览器下载、后端/真实 PostgreSQL、维护组合兼容、发行构建、域名/DNS/TLS、ECS 或部署验收。未新增费用、读取凭据、安装、迁移、提交或推送。

后续可将这两文件补丁集成到核定的 `fe97563` 后继候选。源码同步和维护保留另行安排；正式打包不能为满足干净检查而删除现有未提交维护源码。本轮没有发现需修复的候选生产缺陷。
