# ReqPilotX Frontend

这是 ReqPilotX 唯一浏览器端工程。旧 `app/static` 页面与控制器已经删除；
不要重新建立第二套 HTML/JavaScript 入口。

## 技术与边界

- React 19 + TypeScript；
- Vite 8 负责开发服务、测试和 production build；
- `src/app` 负责应用入口、偏好和错误边界；
- `src/features` 按 projects、documents、preparation、review、assessment、
  matrix、exports、settings、restoration、pdf 分域；
- `src/shared` 负责同源导航、API/runtime contracts、hooks 和通用 UI；
- FastAPI 只提供 `/api/*` 与生产环境的 `dist/index.html`、`dist/assets/*`。

浏览器正式路径：

```text
/
/home
/workspace
/extraction-review
/pdf-viewer
```

这些路径共享一个 Vite entry。还原审查和 PDF 查看器按路由懒加载；工作台
URL 中的 project/document/requirement set identity 仍可复制、刷新和恢复。

## 本地开发

需要 Node.js `>=22.12`。从 `prototype/` 运行：

```bash
./start.sh --dev
```

它会启动：

```text
Vite HMR  http://127.0.0.1:5173
FastAPI   http://127.0.0.1:8010
worker    independent durable worker
```

也可单独执行：

```bash
cd frontend
npm ci --no-audit --no-fund
npm run test
npm run typecheck
npm run build
```

`dist/` 是生成产物，不手工编辑。稳定 `./start.sh` 会按 lockfile 同步依赖，
并在源码变化后自动重建；FastAPI 找不到正式 entry 时返回 503，不回退 legacy
页面。

## 验证口径

日常前端改动运行受影响 Vitest、TypeScript 和 production build。完整目标
浏览器、多视口、键盘/辅助技术和大文档性能检查属于定时或正式上线前验收，
不作为普通功能开发的总门禁。

必须保持的产品不变量：

- partial/interrupted 结果只提供后端真实支持的核对、反馈和恢复动作，不伪造能力或把局部当作全文；
- SourceIR 与 source span 不由浏览器重写；
- split/merge/exclude/reinclude 使用 Unicode code-point 与来源 lineage；
- Structure revision/undo、Requirement Set、Evidence 和 PDF 框标保持确切身份；
- Qdrant 或远端 AI 不可用只限制对应 profile，不阻断核心工作流。
