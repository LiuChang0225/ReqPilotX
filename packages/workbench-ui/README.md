# Shared workbench UI

This is the single source for business components, types, API clients, host
contract and styles. Web imports the `./web` composition surface; host adapters
consume the generated `dist-panel` CJS/ES/CSS and `host-contract.json`.

From the repository root: `npm ci`, `npm test`, `npm run typecheck`,
`npm run build:panel`, `npm run test:css`. The root lock pins the same dependencies
as the former frontend. React is external in Panel builds and supplied by the host.
Contract `reqpilot-host-ui/v1` remains major 1 / minor 1.
