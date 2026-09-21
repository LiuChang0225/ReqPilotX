# ReqPilotX server

This package owns the API, durable worker, configuration, persistence, object
storage and concrete Agent-host adapters. It depends on `reqpilotx-engine` and
never carries a second copy of the engine implementation.

Installed entrypoints are `reqpilotx-api` and `reqpilotx-worker`. The legacy
`prototype/start.sh` and `prototype/app` imports remain development compatibility
routes while R4 migrates the outer application packaging.

Writable state is always host configured with `REQPILOT_STORAGE_DIR` and
`REQPILOT_OBJECT_ROOT`; installed code never writes beside site-packages.
`REQPILOT_FRONTEND_DIR` selects the separately built frontend. Checkout-only
Agent Kit and development-source bundle operations require the explicit absolute
`REQPILOT_DEVELOPMENT_ROOT`. PostgreSQL migrations and the App Server protocol
lock are read-only package resources.
