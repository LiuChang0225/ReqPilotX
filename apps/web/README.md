# Web shell

Owns page routing, DOM startup and Vite environment configuration. Business UI
comes from `@reqpilotx/workbench-ui`; no business source is copied here.

From the repository root: `npm ci`, `npm run dev`, `npm run build`.
`REQPILOT_API_ORIGIN` selects the development proxy. Production output is
`apps/web/dist`; pass its absolute path as `REQPILOT_FRONTEND_DIR` to the server.
Routes and source/work/revision semantics remain unchanged.
