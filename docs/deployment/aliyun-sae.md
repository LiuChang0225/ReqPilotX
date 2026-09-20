# 阿里云部署参考：后置工程资产

更新：2026-09-03。状态：保留方案与脚本，未声明云端部署或新 Agent 生产资格已经完成。
当前先做目标态验证和可移植能力沉淀；只有明确要求部署时才使用本页。

## 已保留的方案

现有代码支持 SAE API/worker、RDS PostgreSQL、私有 OSS 的部署方向：
API 与独立 worker 消费同一业务用例、数据库和对象存储。
这是现有应用的可选部署参考，不限制第一阶段探索框架。

```text
SAE API / SAE worker
       |
RDS PostgreSQL + private OSS
```

相关脚本位于 `prototype/deploy/aliyun/`：
`migrate.sh`、`worker-start.sh`、`api-start.sh`。

## 部署前必须重新核对

- 用户的发布授权、目标账户/地域、网络、访问控制和客户数据使用范围。
- 当前应用与 Agent 的业务验收、目标宿主/协议支持、登录和依赖分发方式。
- 数据库与对象存储的一致性备份、恢复和正式迁移窗口。
- UI 构建、任务恢复、权限、安全、容量与端到端业务交付。
- 必要的 Agent 登录/工具/视觉环境是否可在目标部署方式下合规且可靠地运行。

不能只凭 API/worker 能启动，就认定新 Agent 全文工作流可以生产使用。
不创建云资源、不上传客户资料、不复制本机认证，除非当前任务明确授权。

## 配置索引

云模式显式设置 `REQPILOT_ENV=cloud`，配置 PostgreSQL URL、OSS 地域/bucket/prefix/endpoint、
批准的 Host/Origin 与私有 scratch root。模板见 [prototype/.env.example](../../prototype/.env.example)。

OSS 凭据应来自受控的最小权限临时身份；过期轮换与秘密管理在目标环境验证，
不将长期 AccessKey、数据库密码或 token 放入代码、命令参数或日志。
Qdrant 只在明确需要检索时部署，不作为核心数据存储的前置。

## 数据与回滚

当前开发机已使用 PostgreSQL/LocalObjectStore。实际上云时从当前一致性快照迁移，
不得重新导入冻结的旧 SQLite 覆盖新数据。
对象保留相同 key、size、hash 和来源身份；未通过验证的迁移不能开放新写入。

云端开始写入后，恢复必须使用同一时点的数据库/对象备份或向前修复，
不能通过切回 SQLite 伪装回滚。现有历史迁移工具仅供明确的数据恢复任务使用。

日常运行见[本地部署](local-native-postgresql.md)；开发顺序见[开发计划](../需求提取/开发计划.md)。
