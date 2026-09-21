# Frontend compatibility entry

Authoritative sources moved to `packages/workbench-ui` and `apps/web`.
Run `npm ci` at the repository root. Existing `npm --prefix prototype/frontend
run build|build:panel|test|typecheck|dev` commands forward to the root workspace.
Outputs live only at `apps/web/dist` and `packages/workbench-ui/dist-panel`;
this directory contains no second UI implementation or copied build output.

Remove this compatibility entry after remaining external launch scripts adopt
the root npm workspace commands and explicit `apps/web/dist` frontend setting.
