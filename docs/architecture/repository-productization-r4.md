# R4：共享工作台与宿主发行边界

基线为 R3 `74f5705ad01bb2c9f86f9472850c81b9f10c7aaa`。保留已有桌面提交
`fb0236de` 及合入提交 `8b82f67a`。R4 不包含 R5 资产修复，不改业务算法、
API schema、SQL、R1 发行保护或 R2 固定能力包身份。

## 唯一源码与产物

| 原位置 | 权威位置 | 消费者与兼容 |
|---|---|---|
| prototype/frontend/src 的业务组件、API、类型、样式、host | packages/workbench-ui/src | Web 通过公开 `./web` 入口组装；Panel 从同一源码构建 |
| prototype/frontend/src/main.tsx、app/App.tsx、路由测试 | apps/web/src | 独立 Web DOM/路由/环境装配 |
| prototype/frontend 的锁、依赖与测试配置 | 根 package.json/package-lock.json/tsconfig.json/vitest.config.ts | 单一 npm workspace；原依赖版本保留，测试包含两处全部原测试 |
| prototype/frontend 的 Panel 配置、CSS scope 脚本 | packages/workbench-ui | dist-panel 输出 CJS/ES/CSS/host-contract.json |
| prototype/app_desktop.py、build_desktop.py、spec、发布脚本/清单 | apps/desktop | 旧入口薄转发；没有第二份桌面实现 |
| Pair 的默认 checkout/venv 启动 | integrations/pair/host/src/managed_api.py | `-I` 的非 editable engine/server 安装；无顶层 app 或 checkout 兜底 |

Web 产物为 `apps/web/dist`，Panel 为 `packages/workbench-ui/dist-panel`。
`reqpilot-host-ui/v1` major 1/minor 1 及六个 required exports 保持；React/JSX/ReactDOM
由宿主提供。CSS scope、动画命名隔离、卸载清理、transport、Range 与版本响应头保持。
旧 `prototype/frontend` 只转发 npm 命令；调用者改用新入口后可移除。根 npm 安装
后在旧目录运行 build/test/typecheck 仍有效；旧目录不生成第二份源码或 dist。

## 安装与启动

```sh
npm ci
npm test
npm run typecheck
npm run build
npm run build:panel
npm run test:css
node integrations/pair/scripts/build.mjs --shared-ui /absolute/built/dist-panel
```

Pair 的 Cordis 配置接收 `python`、`frontendRoot`、`postgresBinDir`、`dataRoot`。
对应环境变量是 `REQPILOT_PAIR_PYTHON`、`REQPILOT_PAIR_FRONTEND_ROOT`、
`REQPILOT_PAIR_POSTGRES_BIN`、`REQPILOT_PAIR_DATA_ROOT`。Python 与前端须为绝对路径，
先由操作者安装非 editable wheel。运行时检查安装来源、37 个包内迁移与前端入口；
缺依赖/产物明确失败，不安装或降级到 checkout。

```sh
uv venv /absolute/private/venv
uv pip install --python /absolute/private/venv/bin/python /release/reqpilotx_engine-VERSION-py3-none-any.whl /release/reqpilotx_server-0.1.0-py3-none-any.whl
```

Pair 使用规范 `RuntimeSettings` / `build_runtime_container(..., apply_migrations=True)`
与 `reqpilotx_server.main:app`，wrapper 只负责安装检查、环境与动态 loopback socket。
明确设置 storage/object/scratch/frontend，隔离继承的应用与模型配置。私有 PostgreSQL
和数据租约保持；停止顺序 API → DB，仅操作本实例句柄。未知非空目录、相对/链接
数据根、竞争 owner 拒绝。开发只读连接使用显式 connect mode。

桌面在 `apps/desktop/.venv` 准备原 requirements 和 engine/server wheel 后，运行
`apps/desktop/.venv/bin/python apps/desktop/build_desktop.py`。输出位于
`apps/desktop/dist`。规范包动态模块、包数据、migrations/protocol lock 和 Web
产物继续显式收集。旧 `prototype/build_desktop.py` 转发到相同构建器。

## 验证与证据

续作验证记录保存在 ignored `output/r4-continuation/`，不含客户资料。

- A：82 个测试文件、897 项前端测试通过；typecheck、Web/Panel production build、
  4 项 CSS scope 通过。迁移后两项合成 fixture 引用最初未解析，修正到原有
  prototype/contracts 的唯一 fixture 后全部通过。
- B：13 项 Pair 合同通过，包括独立 React 18 模块宿主的样式卸载（不等于外部 Pair UI）。
  relocation 测试将共享产物与 adapter 构建源复制到临时目录，构建 identity 一致，
  缺失/不兼容合同拒绝。真实 installed smoke：仓库外非 editable wheel，37 个迁移，
  合成项目/文档、原件字节、固定成果/导出及重启 ID/快照连续性通过。
  `pair-installed-receipt.json` 记录安装来源、实际身份和 HTTP 结果。
- C：`f767603` 上完成 apps/desktop 实际 macOS arm64 构建（Python 3.13.2，
  PyInstaller 6.20.0，macOS 27）与 artifact self-test。runtime/full readiness、
  静态资源、Host/Origin、优雅退出/锁释放、Qdrant 状态保持全部通过。
  `desktop-build.log`、`desktop-artifact-receipt.json`、`desktop-artifact-manifest.json`
  保存实际构建 SHA/环境/hash。后续仅文档和测试 fixture 修订不冒充重建。
- D：保留全部原后端、插件/MCP、发行和 PostgreSQL smoke CI；新增非 editable Pair
  实际进程链与 React 18 fixture 检查。PR/最终 head CI 在交付时记录。

源码审计曾因测试迁移失去原路径/hash 例外而报出两个合成 fixture：URL userinfo 和
无效测试 token。URL 改用标准 example.invalid，token 以运行时合成保留完全相同
测试字节及断言；未放宽审计或修改全局例外清单。桌面迁移的聚焦 Python 回归共
88 项通过；必要 fixture 修订后再跑其 44 项预检和 PDF URL 聚焦测试。

独立托管进程复现（fixture 由原 worked-example 生成，adapter 和 Web 已复制到仓库外）：

```sh
node /temporary/pair/scripts/installed-smoke.mjs /temporary/venv/bin/python /temporary/web /postgres/bin /synthetic/example /output/pair-installed-receipt.json
```

桌面自检使用 test/SQLite/local object store、readiness、静态资源、Host/Origin、
锁释放与优雅退出，不能替代 PostgreSQL worker 验收。Pair 无 worker 或模型调用。
外部 Pair UI、完整 Agent 会话绑定/进度/业务写入、正式签名、公证、Windows/Linux
成品及 fresh-machine 未验；原有这些能力的边界保持。

## 回滚与集成导航

合并前可直接弃用 R4 分支；合并后按依赖顺序 revert R4 的消费者、桌面、Pair、UI
提交，再 `npm ci` 重建原前端。回滚不处理数据库或用户 runtime-data，不重算冻结
能力包 identity。运行中的自有 API/DB 先按宿主正常停机后切换程序版本。

供 R5/集成负责人使用的根导航小段：
“Web 入口见 apps/web/README.md；共享工作台见 packages/workbench-ui/README.md；
桌面见 apps/desktop/README.md；完整迁移合同见 docs/architecture/repository-productization-r4.md。”
本线不修改根 README/AGENTS。
