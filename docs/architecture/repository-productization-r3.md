# R3 共享源码与服务装配迁移

R3 基线为 `cae06758d5189734337f0b8c53c544aa00c266b7`。该提交包含已合并的
R1 构建输入保护 `86437a17` 和独立 R2 固定能力包提交。

## 路径映射

| 旧路径 | 权威路径 | 兼容策略 |
|---|---|---|
| `prototype/app/{domain,application,ports,...}` 中的 R1 便携闭包 | `packages/engine/src/reqpilotx/` | 对应旧模块为薄模块别名；不保留算法副本 |
| `prototype/scripts/fast_source_capture.py` | `reqpilotx.tools.fast_source_capture` | 旧脚本只调用安装包 `main()` |
| Codex 具体模块中的 `CanonicalStructureHandoffBindingV1` | `reqpilotx.ports.agent_structure_handoff_executor` | 具体宿主模块重导出同一类，wire schema 不变 |
| API、worker、配置、数据库、对象存储和 Agent 宿主 | `apps/server/src/reqpilotx_server/` | R3-B 迁移后，`prototype/app` 仅保留兼容别名 |
| `prototype/migrations` 等只读宿主资源 | `apps/server/src/reqpilotx_server/resources/` | R3-C 通过包资源定位，旧入口仅兼容转发 |

依赖方向固定为 `reqpilotx_server → reqpilotx`。Engine 不导入 server、API、worker、
数据库或具体 Agent 宿主。旧冻结 bundle 的 `runtime/prototype/app` 仍按原身份读取；
新 bundle 使用 `runtime/prototype/reqpilotx`，不会改写旧包。
