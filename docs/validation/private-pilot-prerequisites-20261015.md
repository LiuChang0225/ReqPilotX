# 域名可访问前的本地发行与恢复准备

2026-10-15。依据用户 06:24:01 UTC“那就先把域名可访问前的前置步骤都完成”，由本任务唯一集成。正式发行、PG/非空 Agent、备份恢复及服务器验收分别记证据，不提前标记全项通过。

## 固定输入与保留现场

起点为 `fe97563eb6eec6537f821fd840cbf5a367acee8b`，独立本地 Git checkout 先仅集成已验证的两文件 UI 增量，形成 `c5cf26a41909accbce547f268480378a3f2b2942`。2,681 个跟踪 blob 对 Git 内容核验通过，workspace UI 实际解析到本候选源码。无远端、未 push/merge。主工作区仍为 `f18cf939…`，11 个未提交维护文件不移动、不暂存、不混入发行。

实际环境为现有 Node 26.8.2 / Vitest 4.1.10 / uv 0.12.13；129 项 UI 回归、TypeScript 与 Vite 构建已通过。Vite 有大于 500kB chunk 提示，不据此宣称性能验收。前一轮 Node 25.6.1 的成绩是历史，未混用。

## 已修正的发行接线

能力包已存在于发行的 `capability/reqpilot`，原模板却未提供注册和 exact bundle pin，默认 registry 还在 application 备份及 systemd 写路径外。本候选沿现有正式 CLI 补齐注册说明，将 registry 明确置于 `application/native-loop-capabilities`。provider-tool-loop 预检要求 registered-release、明确 hash、同备份范围内 registry，并核对完整 bundle 与已安装 engine 的身份。Agent 未启用不声明能力或模型就绪。部署边界、错误配置、篡改/额外文件及只读状态输出有聚焦回归。

此处没有在现有 ECS 注册或启用能力，注册是待核实目标和备份后才能执行的持久写操作。旧 hash 目录须保留以支持旧任务恢复。

## 用户原会话的只读脚本

源码为 `deploy/saas/status-readonly.py`；同目录 `status-readonly-command.sh` 是其可直接粘贴的 `python3.11` heredoc，不在服务器创建文件。用户只在原已登录服务器会话自行执行，不需要 sudo、新登录或新授权。当前电脑工具已实际安全拒绝访问 Terminal，Agent 不以 shell、另一终端、SSH 或远程连接替代。

只读固定 `/opt/reqpilotx/current`、其合法 release manifest、`/var/lib/reqpilotx/application` 所在文件系统及 `/var/backups/reqpilotx` 规范快照目录；systemd 只读三个既定 unit 的少量状态。无网络，不读取 env/配置全文、凭据、journal、数据库或客户正文。权限不足、路径/格式不符或命令失败标 UNKNOWN，不切换路线。不以文件存在证明备份完整或恢复成功，也不以实例健康证明应用健康。

输出的发行 hash、内部路径、服务状态、可用磁盘和备份时间/大小只给授权维护者；无需附 cookie、凭据、配置原文、日志或业务文件。

## 当前云证据及尚需验收

父会话官方控制台读回：14:20 Asia/Shanghai 备案管局审核中、尚无备案号且无待补材料/操作；指定北京 ECS 运行健康，公网 39.107.33.111 与订单一致。14:26 北京时间显示 1 块 40GiB 系统盘、关联快照 0 / 策略 0，未安装云监控、未取得磁盘使用量；云盘检测显示云助手未授权。自动化运维已安装扩展 0 / 安装记录无数据，不证明应用未部署。云快照为零不等于没有应用备份。active release、应用健康、备份及实际恢复仍未知。不要创建收费快照或授权云助手。

服务器只读来源：`https://ecs.console.aliyun.com/server/i-2zehtfdx0nydwhj37oz4/disk?regionId=cn-beijing` 与 `/automationOps?regionId=cn-beijing`，由父会话交接，本任务未重新访问。

正式 wheel、bundle、release manifest 与 PG 验收必须绑定同一最终本地提交。完整 Linux 依赖 wheelhouse 缺失时不能宣称可部署包完成。未执行的 PG、非空合成 Agent/同版真实 XLSX、安装后运行、同一时点 DB+application/registry 隔离恢复、员工浏览器和目标 ECS 验收保持独立缺项；已有工具入口不记作本轮 PASS。

新增依赖下载曾被自动审批拒绝，理由为未获明确下载授权且此前禁止新增安装；该路径暂停等待精确批准，不换源或工具绕过。允许继续使用已安装依赖做离线构建/测试，不将此说成新增下载已获准。实际构建/验收和失败日志、hash、最终提交身份另存外部交付回执。
