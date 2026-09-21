# ReqPilotX 现有应用运行

本目录保留开发启动脚本和旧 Python import/前端命令兼容入口。
React/Vite Web 外壳位于 `apps/web`，共享业务 UI 位于 `packages/workbench-ui`，
桌面启动与打包位于 `apps/desktop`。
共享业务源码位于 `packages/engine/src/reqpilotx`，FastAPI、独立 worker、PostgreSQL、
对象存储和 Agent 宿主位于 `apps/server/src/reqpilotx_server`。
它是目标态探索可复用的工程资产，不意味着新 Agent 的全文条目/分类/拆分已完整验收。
当前开发顺序见[开发计划](../docs/需求提取/开发计划.md)。

桌面旧构建入口转发到 `apps/desktop`，直接组合上述两个权威包；
`prototype/app` 不进入正式桌面依赖图。发布预检扫描权威 runtime 源码与前端构建，
PyInstaller 显式收集引擎动态兼容转发所需的 `reqpilotx` 子模块、引擎包数据和
服务只读资源，并把冻结前端目录注入安装版运行设置。旧命名空间只保留给尚未迁移的
开发脚本和测试。该源码边界检查不替代目标系统上的完整 PyInstaller、签名、启动和
数据恢复验收。

## 本地启动

需要 Node.js >=22.12、Python 虚拟环境与本机 PostgreSQL。首次准备：

```bash
createdb reqpilotx
cd prototype
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cd ..
prototype/.venv/bin/pip install -e packages/engine -e apps/server
cd prototype
./start.sh
```

已有数据库不要重复创建或迁移。当前开发机已完成 SQLite → PostgreSQL/LocalObjectStore 切换，
禁止用旧 SQLite 覆盖当前数据；SQLite 只供隔离测试和历史迁移工具。

默认本地服务是 [工作台](http://127.0.0.1:8000/workspace)，数据库为本机 reqpilotx，
不默认连接 RDS/OSS。真实配置通过 shell 或密钥管理工具注入；[.env.example](.env.example)
只是模板，不自动加载，也不包含真实凭据。

```bash
./status.sh
./stop.sh --drain
./start.sh --drain-restart
./start.sh --dev
.venv/bin/python scripts/runtime_admin.py doctor
```

现有运行身份不一致时，启动器会拒绝新前端/旧 worker 混跑；排空让在手任务完成后退出。
开发模式提供 Vite 5173 与 API 8010。常规启动复用/生成 apps/web/dist，不提交构建产物。
具体 worker、Qdrant、备份与恢复边界见[本地部署](../docs/deployment/local-native-postgresql.md)。

## Codex 运行配置

现有 AI 阅读和 Agent 文档路径使用本机 Codex App Server；读取与需求提取的业务状态分开。
相关配置由代码读取：

```text
REQPILOT_CODEX_BIN
REQPILOT_CODEX_HOME
REQPILOT_AGENT_MODEL_ID
REQPILOT_READING_AI_TIMEOUT_SECONDS
REQPILOT_READING_DIRECT_INPUT_TOKEN_BUDGET
```

需要真实可用的 Codex 运行环境、批准的登录与目标模型。存在 Azure/百炼配置不等于 Codex 可运行，
也不会自动退回旧 provider。不要复制他人的认证文件或降低目录/来源权限来绕过就绪检查。
模型、协议与依赖版本以实际环境、Kit 锁和回执为准，不在这里复制易过期版本。

第一阶段的 Codex App 原生探索不要求先完成所有 App Server 自动任务入口。
当前 F06 局部结构可以复用，不能把局部产品发布当成全文自动处理。

## 应用入口

- `/workspace?task=projects`：项目、客户来源与知识文件。
- `task=reading`：单文档独立 AI 阅读，不向正式需求链隐式写入。
- `task=extraction&mode=organize|review`：原文结构、条目和核对。
- `task=analysis`：需求与证据、AI/人工结论。
- `task=export`：版本、比较、DRAFT/FORMAL 和下载。
- `/api/runtime-readiness`：基础运行状态；Agent 能力另有就绪检查。
- `/openapi.json`：当前进程的 API schema，不以历史文档固定路由数量作真值。

前端命令见[Web README](../apps/web/README.md)；
接口与工件位置见[数据/API 索引](../docs/sor-ai-app-data-api-v1.md)。

## 测试

从仓库根：

```bash
prototype/.venv/bin/python scripts/test_python.py backend
npm test
npm run typecheck
npm run build
```

保留正确 top-level，使历史兼容测试正常导入。测试使用隔离 profile，不重置生产数据。
只改局部时选择聚焦测试；完整 corpus、真实模型、外部连接器与发布验收按任务另行授权。
详见[测试与评测计划](../docs/需求提取/测试与评测计划.md)。

## 保留的后置能力

备份/迁移、桌面打包、云部署、兼容 provider 与历史 canary 保留供明确任务使用，
不在当前目标态尚未验收时扩建或自动调用。
[阿里云部署参考](../docs/deployment/aliyun-sae.md)不表示已经部署或仅差一个账号。
