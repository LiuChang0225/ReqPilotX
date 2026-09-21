# R3 共享源码与服务装配迁移

R3 基线为 `cae06758d5189734337f0b8c53c544aa00c266b7`。该提交包含已合并的
R1 构建输入保护 `86437a17` 和独立 R2 固定能力包提交。

## 路径映射

| 旧路径 | 权威路径 | 兼容策略 |
|---|---|---|
| `prototype/app/{domain,application,ports,...}` 中的 R1 便携闭包 | `packages/engine/src/reqpilotx/` | 对应旧模块为薄模块别名；不保留算法副本 |
| `prototype/scripts/fast_source_capture.py` | `reqpilotx.tools.fast_source_capture` | 旧脚本只调用安装包 `main()` |
| Codex 具体模块中的 `CanonicalStructureHandoffBindingV1` | `reqpilotx.ports.agent_structure_handoff_executor` | 具体宿主模块重导出同一类，wire schema 不变 |
| API、worker、配置、数据库、对象存储和 Agent 宿主 | `apps/server/src/reqpilotx_server/` | `prototype/app` 仅保留兼容别名；安装入口为 `reqpilotx-api` / `reqpilotx-worker` |
| `prototype/migrations` 等只读宿主资源 | `apps/server/src/reqpilotx_server/resources/` | 包资源定位；旧 migration 路径为指向同一文件的兼容 symlink |

依赖方向固定为 `reqpilotx_server → reqpilotx`。Engine 不导入 server、API、worker、
数据库或具体 Agent 宿主。旧冻结 bundle 的 `runtime/prototype/app` 仍按原身份读取；
新 bundle 使用 `runtime/prototype/reqpilotx`，不会改写旧包。

旧 `python -m app.worker` 入口显式调用同一 server `main()` 并传播退出码；
普通模块 import 仍保留别名身份。能力包 `inspect` 与任务准备共用受控注册路径
解析，旧 capability 脚本仅转发到 server CLI，不维护第二份检查实现。

后继 Web、共享工作台、Pair 安装版和桌面归属见
[R4 交付说明](repository-productization-r4.md)。

可写状态只由 `REQPILOT_STORAGE_DIR`、`REQPILOT_OBJECT_ROOT`、capability registry
和任务私有根决定；前端由 `REQPILOT_FRONTEND_DIR` 指定。需要 checkout 中 Agent Kit
或显式源码构建时必须设置 `REQPILOT_DEVELOPMENT_ROOT`，发行缺包不会据此自动降级。
